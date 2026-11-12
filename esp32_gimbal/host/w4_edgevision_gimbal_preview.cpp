// ============================================================================
//  w4_edgevision_gimbal_preview.cpp —— 视觉追踪云台 + 实时预览（一体化）
//
//  为什么要有这个文件
//  ------------------
//  原来的分工是：
//      w4_edgevision_gateway  (C++)  → 独占 /dev/video81，跑 NPU，推 9000 TCP
//      gimbal_tracker.py      (Py)  → 连 9000 拿框，算角度，发串口
//  两者是**两个进程**，而 UVC 摄像头**不支持并发打开**。所以"边看画面边追踪"
//  在原架构下不可能：开预览就抢不到摄像头，跑网关就看不到画面。
//
//  本文件把三件事塞进**一个进程**：
//      ① 读 /dev/video81（云台上那颗）
//      ② 跑 NPU 检测（复用 postprocess_dfl_free）
//      ③ MJPEG 推 8080 网页 + 串口驱动 ESP32 云台
//  这样画面和追踪用的是**同一路帧**，所见即所得。
//
//  控制律：复刻 host/gimbal_control.py 的 compute_control（增量式）
//      dx = cx - 0.5      → 目标在画面右侧则 dx>0
//      d_pan  = +dx * fov_pan * kp_pan
//      d_tilt = -dy * fov_tilt * kp_tilt
//      死区、单帧限速、顶限位不推 —— 三条都和 Python 版一致
//
//  ⚠️ invert-pan 是实测结论，不是可选参数
//      2026-11-03 三位置采样实测（pan 固定 -15/0/+15，各采 10 帧 person 框）：
//          cx = 746.9 / 878.6 / 947.2 px   →   Δcx = +200.4 px
//      云台右转，画面里目标**也右移** = 正反馈。不翻转会几秒内顶死限位。
//      所以默认 invert_pan = true。硬件改动后要重测（见 README 8.6.1）。
//
//  编译（板端）：
//      g++ -O2 -std=c++17 w4_edgevision_gimbal_preview.cpp \
//          /root/edgevision/deploy/cpp/src/postprocess_dfl_free.cpp \
//          -o w4_edgevision_gimbal_preview \
//          -I/root/lab -I/root/edgevision/deploy/cpp/include \
//          -I/root/edgevision/deploy/cpp/3rdparty/rknn_api/include \
//          `pkg-config --cflags --libs opencv4` -lrknnrt -lpthread
//
//  运行：
//      ./w4_edgevision_gimbal_preview /dev/video81 --gimbal /dev/ttyUSB0
//      不带 --gimbal 就是纯预览（等价于原来的 preview_real）
// ============================================================================
#include <iostream>
#include <vector>
#include <queue>
#include <chrono>
#include <cstring>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <unistd.h>
#include <fcntl.h>
#include <termios.h>
#include <pthread.h>
#include <csignal>
#include <sys/socket.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <arpa/inet.h>      /* inet_pton：TCP 模式下解析桥接服务地址 */
#include <opencv2/opencv.hpp>
#include <rknn_api.h>

// ----------------------------------------------------------------
//  9000 检测结果协议（与 lab/w4_edgevision_gateway.cpp 逐字节一致）
//  为什么必须一致：Agent（lab/agent/rkllm_embodied_agent.cpp）和所有
//  既有客户端都按 248 字节定长帧解析。改了协议就是改接口契约，
//  所有下游要跟着改 —— 所以这里原样复刻，不"优化"。
// ----------------------------------------------------------------
#define DETECT_PORT 9000
#define MAX_BBOXES  10

#pragma pack(push, 1)
struct BBox {
    int32_t class_id;
    float   score;
    float   x, y, w, h;      // 还原到原始画面像素坐标（1280x720）
};
struct DetectionHeader {
    uint32_t frame_id;
    uint32_t bbox_count;
};
struct FrameResult {
    DetectionHeader header;  // 8 B
    BBox boxes[MAX_BBOXES];  // 240 B  → 总计 248 B
};
#pragma pack(pop)

// 全局运行标志。提前声明：9000 推流线程、HTTP 线程、主循环都要用它，
// 如果放在后面的"原有工具函数"区块里，前面的线程函数就引用不到。
static volatile sig_atomic_t g_running = 1;

#include "postprocess_dfl_free.h"
#include "bev_projector.hpp"

using namespace edgevision;

// ---------------------------------------------------------------- 串口云台 --
//
// 协议（与 main/gimbal_main.c 一致）：
//   发： "P <pan> <tilt>\n"  设相对中位角度   "H\n" 回中
//   收： "OK <pan> <tilt>\n"  或 "ERR <code> <msg>\n"
//
// 为什么串口要单独一个线程：
//   115200 下一条命令往返 ~2ms，但舵机缓动+驱动可能让 readline 等到 500ms。
//   如果放在主循环里，推理帧率会被串口拖死（实测会从 21ms 掉到 500ms/帧）。
//   这里做成"生产者-消费者"：主循环只写最新目标角度，串口线程按自己的节奏发。
class GimbalSerial {
public:
    GimbalSerial() : fd_(-1), pan_(0.f), tilt_(0.f), errors_(0), sent_(0) {
        pthread_mutex_init(&lock_, nullptr);
        pthread_cond_init(&cond_, nullptr);
    }
    ~GimbalSerial() {
        running_ = false;
        pthread_cond_broadcast(&cond_);
        if (thread_) pthread_join(thread_, nullptr);
        if (fd_ >= 0) close(fd_);
        pthread_mutex_destroy(&lock_);
        pthread_cond_destroy(&cond_);
    }

    bool open(const char* dev) {
        // 注意：不要 O_NONBLOCK —— 配合 termios VMIN/VTIME 才好读行
        fd_ = ::open(dev, O_RDWR | O_NOCTTY);
        if (fd_ < 0) { perror("open gimbal"); return false; }

        struct termios tio;
        if (tcgetattr(fd_, &tio) != 0) { perror("tcgetattr"); close(fd_); fd_ = -1; return false; }
        cfmakeraw(&tio);
        cfsetispeed(&tio, B115200);
        cfsetospeed(&tio, B115200);
        tio.c_cflag |= (CLOCAL | CREAD);
        tio.c_cflag &= ~CRTSCTS;
        tio.c_cc[VMIN] = 0;
        tio.c_cc[VTIME] = 3;        // 0.3s 读超时，够一条回复
        if (tcsetattr(fd_, TCSANOW, &tio) != 0) { perror("tcsetattr"); close(fd_); fd_ = -1; return false; }

        tcflush(fd_, TCIOFLUSH);
        usleep(200000);
        tcflush(fd_, TCIFLUSH);
        printf("[gimbal] 串口已打开 %s @115200\n", dev);
        running_ = true;
        pthread_create(&thread_, nullptr, &GimbalSerial::thread_entry, this);
        return true;
    }

    // ── TCP 模式：连到 gimbal_bridge.py（它用 pyusb 控 ESP32）────────────
    //
    // 为什么要有这条路（2026-11-04）：
    //   EVM3588-B 内核没编 CONFIG_USB_ACM，ESP32 的 USB-JTAG CDC 建不出
    //   /dev/ttyACM0；TTL 那条路又因 GPIO43 那边的线/针问题不通。
    //   pyusb 在用户态直接驱动 CDC-ACM 已实测可行（Q→S 0.0 0.0 1），
    //   但 C++ 侧没有 libusb 头文件（只有 .so.0，编不过）。
    //   于是让 Python 做 USB 那一段，C++ 用 localhost TCP 跟它说话。
    bool open_tcp(const char* host, int port) {
        fd_ = socket(AF_INET, SOCK_STREAM, 0);
        if (fd_ < 0) { perror("socket"); return false; }

        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons((uint16_t)port);
        if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
            fprintf(stderr, "[gimbal] 非法地址 %s\n", host);
            close(fd_); fd_ = -1; return false;
        }
        if (connect(fd_, (sockaddr*)&addr, sizeof(addr)) < 0) {
            fprintf(stderr, "[gimbal] 连不上桥接服务 %s:%d（先起 gimbal_bridge.py）\n",
                    host, port);
            close(fd_); fd_ = -1; return false;
        }
        printf("[gimbal] 已连接桥接服务 %s:%d（USB CDC）\n", host, port);
        running_ = true;
        pthread_create(&thread_, nullptr, &GimbalSerial::thread_entry, this);
        return true;
    }

    // 主循环调用：只更新目标，不做 I/O
    void set_target(float pan, float tilt) {
        pthread_mutex_lock(&lock_);
        want_pan_ = pan;
        want_tilt_ = tilt;
        has_target_ = true;
        pthread_cond_signal(&cond_);
        pthread_mutex_unlock(&lock_);
    }

    // 主循环调用：读当前状态（给 HUD 显示）
    void state(float& pan, float& tilt, int& errors, int& sent) {
        pan = pan_; tilt = tilt_; errors = errors_; sent = sent_;
    }

private:
    static void* thread_entry(void* self) {
        static_cast<GimbalSerial*>(self)->loop();
        return nullptr;
    }

    void loop() {
        while (running_) {
            pthread_mutex_lock(&lock_);
            while (running_ && !has_target_) pthread_cond_wait(&cond_, &lock_);
            if (!running_) { pthread_mutex_unlock(&lock_); break; }
            float tp = want_pan_, tt = want_tilt_;
            has_target_ = false;
            pthread_mutex_unlock(&lock_);

            // 与上次下发相比几乎没变就不发，省带宽也省舵机寿命
            if (fabsf(tp - pan_) < 0.05f && fabsf(tt - tilt_) < 0.05f) continue;

            char cmd[64];
            int n = snprintf(cmd, sizeof(cmd), "P %.2f %.2f\n", tp, tt);
            if (write(fd_, cmd, n) != n) {
                errors_++;
                printf("[gimbal] 写串口失败\n");
                continue;
            }
            sent_++;

            // 读回复（有超时，不会卡死）
            char buf[128] = {0};
            int got = read(fd_, buf, sizeof(buf) - 1);
            if (got <= 0) { errors_++; continue; }
            if (strncmp(buf, "OK", 2) == 0) {
                float p, t;
                if (sscanf(buf + 2, "%f %f", &p, &t) == 2) { pan_ = p; tilt_ = t; }
                else { pan_ = tp; tilt_ = tt; }
            } else if (strstr(buf, "out_of_range")) {
                // 固件已夹紧执行，不算致命；但要记下来
                pan_ = tp; tilt_ = tt;
            } else {
                errors_++;
            }
        }
    }

    int fd_;
    pthread_t thread_ = 0;
    pthread_mutex_t lock_;
    pthread_cond_t cond_;
    bool running_ = false;
    bool has_target_ = false;
    float want_pan_ = 0.f, want_tilt_ = 0.f;
    float pan_ = 0.f, tilt_ = 0.f;     // 上一次确认的实际角度
    int errors_ = 0;
    int sent_ = 0;
};

// ---------------------------------------------------------------- 9000 推流 --
//
// 与 gateway 的区别（重要）：gateway 用的是「队列 + 单一消费者」模型，
// 一个客户端连上就把队列吃空了，第二个客户端会饿死。
// 这里改成「最新帧广播」：主循环只维护 g_latest_det 一份，
// 每个客户端连接自己记 last_seq，有新帧就发。多客户端互不干扰。
static FrameResult g_latest_det;
static bool g_has_det = false;
static uint32_t g_det_seq = 0;
static pthread_mutex_t g_det_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_det_cond = PTHREAD_COND_INITIALIZER;

static void* detect_streamer_thread(void*) {
    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd < 0) { perror("socket 9000"); return nullptr; }
    int opt = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(DETECT_PORT);
    addr.sin_addr.s_addr = INADDR_ANY;
    if (bind(server_fd, (sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("bind 9000 被占用？");
        close(server_fd);
        return nullptr;
    }
    listen(server_fd, 5);
    printf("[+] 检测流(9000)已监听 —— 与 gateway 协议一致，248B 定长帧\n");

    while (g_running) {
        timeval tv{1, 0};
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(server_fd, &fds);
        if (select(server_fd + 1, &fds, nullptr, nullptr, &tv) <= 0) continue;

        sockaddr_in cli{};
        socklen_t clen = sizeof(cli);
        int c_fd = accept(server_fd, (sockaddr*)&cli, &clen);
        if (c_fd < 0) continue;

        // 每个客户端独立发送：记自己的 last_seq，只发新帧
        uint32_t last_seq = 0;
        while (g_running) {
            FrameResult res;
            pthread_mutex_lock(&g_det_lock);
            while (g_running && g_det_seq == last_seq)
                pthread_cond_wait(&g_det_cond, &g_det_lock);
            if (!g_running) { pthread_mutex_unlock(&g_det_lock); break; }
            last_seq = g_det_seq;
            res = g_latest_det;
            pthread_mutex_unlock(&g_det_lock);

            // MSG_NOSIGNAL：客户端断开时不触发 SIGPIPE 杀进程
            if (send(c_fd, &res, sizeof(res), MSG_NOSIGNAL) != (ssize_t)sizeof(res)) break;
        }
        close(c_fd);
        printf("[9000] 一个客户端断开\n");
    }
    close(server_fd);
    return nullptr;
}

// 把本帧检测结果发布给 9000 客户端（主循环调用）
static void publish_detections(uint32_t frame_id, const std::vector<Detection>& dets,
                               int img_w, int img_h) {
    FrameResult res{};
    res.header.frame_id = frame_id;
    uint32_t n = std::min((uint32_t)dets.size(), (uint32_t)MAX_BBOXES);
    res.header.bbox_count = n;
    for (uint32_t i = 0; i < n; ++i) {
        // Detection 的 x1..y2 已经是原图坐标（main 里做过 letterbox 逆变换）
        float x1 = std::max(0.f, std::min(dets[i].x1, (float)img_w - 1.f));
        float y1 = std::max(0.f, std::min(dets[i].y1, (float)img_h - 1.f));
        float x2 = std::max(0.f, std::min(dets[i].x2, (float)img_w - 1.f));
        float y2 = std::max(0.f, std::min(dets[i].y2, (float)img_h - 1.f));
        res.boxes[i].class_id = dets[i].class_id;
        res.boxes[i].score = dets[i].score;
        res.boxes[i].x = x1;
        res.boxes[i].y = y1;
        res.boxes[i].w = x2 - x1;
        res.boxes[i].h = y2 - y1;
    }
    pthread_mutex_lock(&g_det_lock);
    g_latest_det = res;
    g_has_det = true;
    g_det_seq++;
    pthread_cond_broadcast(&g_det_cond);
    pthread_mutex_unlock(&g_det_lock);
}

// ------------------------------------------------------- 控制律（纯函数） --
struct ControlCfg {
    // ── 默认值 = run_tracking.ps1 实测标定的那套（2026-11-03）──────────
    // 这套值在真机上跑过：60 秒 223 条指令、零错误、77 次锁定。
    //
    // 中途我为了压住"冲限位"加过三样东西（settle 暂停 / 低通滤波 / 质量筛选），
    // 结果响应慢到用户能明显感觉到"不灵敏"。现已全部**默认关闭**，
    // 需要时用命令行开关逐个打开（--settle / --smooth / 质量筛选默认关）。
    //
    // 教训：防过冲的手段如果代价是"几乎不响应"，那还不如不过冲。
    //       正确的顺序是先把目标框弄对（人站远/接 tilt），再谈抑制震荡。
    float kp_pan = 0.20f;
    float kp_tilt = 0.15f;
    float fov_pan = 60.0f;
    float fov_tilt = 36.0f;
    float deadzone = 0.05f;
    float max_step = 2.5f;
    float pan_range = 90.0f;
    float tilt_range = 45.0f;
    bool invert_pan = true;     // ★ 两次独立标定确认（见 8.6.1 / 8.7.4）
    bool invert_tilt = true;
    bool enable_pan  = true;    // pan 舵机没接时用 --no-pan 关掉
    bool enable_tilt = true;    // tilt 舵机没接时用 --no-tilt 关掉
    float min_score = 0.40f;

    // ── 以下三项默认关闭，恢复"原来的"响应速度 ─────────────────────
    bool filter_partial = false;   // 质量筛选：关（原来没有）
    float smooth_alpha = 0.0f;     // 低通滤波：关（原来没有）
    float jump_max = 0.25f;        // 仅在 smooth>0 时生效
    int settle_frames = 0;         // settle 暂停：关（原来没有）

    // 3D 反投影框：默认开，但**它假设的安装参数（支架高 1.6m、俯仰 8°）
    // 与实际云台安装不一致时会严重失真**（2026-11-03 实测：人距 <1m 时
    // 2D 框 1014x698 几乎全画面，3D 框却画成 90x280 的小框，距离报 8.0m）。
    // 用 --no-3d 关掉，只看 2D 检测框——调试追踪时 2D 框才是真值。
    bool show_3d = true;
};

static inline float clampf(float v, float lo, float hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

// 返回 true = 落在死区，不需要动
static bool compute_control(const ControlCfg& c, float cx, float cy,
                            float pan, float tilt,
                            float& out_pan, float& out_tilt,
                            float target_scale = 0.f) {
    float dx = cx - 0.5f;
    float dy = cy - 0.5f;

    // ── 死区与步长按目标大小自适应（不引入任何延迟）────────────────
    //
    // 为什么需要：目标离得近时框占画面很大（实测 1014x698 = 79% 画面宽），
    // 身体横向移动几厘米，cx 就跳 200+ px，而固定死区只有 ±64 px，
    // 于是每帧都被判为"超标"→ 反复下发 → 看起来又抖又抽。
    // 反过来目标远时框只占 20%，用大死区又会反应迟钝。
    //
    // target_scale = 框宽 / 画面宽（0~1）。
    //   scale=0.15（3m 外）→ 死区 ×0.7、步长 ×1.3   （灵敏）
    //   scale=0.79（<1m）  → 死区 ×1.9、步长 ×0.6   （稳）
    // 限制在 [0.6, 2.0] 倍，避免极端值。
    float dz_scale = 1.0f, step_scale = 1.0f;
    if (target_scale > 0.01f) {
        // 以 0.35 为"标准距离"（框约占画面 1/3，对应 1.5~2m）
        float ratio = target_scale / 0.35f;
        dz_scale   = clampf(0.6f + 0.4f * ratio, 0.6f, 2.0f);
        step_scale = clampf(1.0f / (0.7f + 0.3f * ratio), 0.5f, 1.3f);
    }
    const float deadzone = c.deadzone * dz_scale;
    const float max_step = c.max_step * step_scale;

    if (fabsf(dx) < deadzone) dx = 0.f;
    if (fabsf(dy) < deadzone) dy = 0.f;
    if (dx == 0.f && dy == 0.f) {
        out_pan = pan; out_tilt = tilt; return true;
    }

    float err_pan = dx * c.fov_pan;
    float err_tilt = dy * c.fov_tilt;

    float d_pan = err_pan * c.kp_pan;
    float d_tilt = -err_tilt * c.kp_tilt;
    if (c.invert_pan) d_pan = -d_pan;
    if (c.invert_tilt) d_tilt = -d_tilt;

    d_pan = clampf(d_pan, -max_step, max_step);
    d_tilt = clampf(d_tilt, -max_step, max_step);

    // tilt 轴没接舵机时强制置零。
    // 为什么必须显式关掉而不是"反正不动就算了"：tilt 舵机不存在 → 指令不产生
    // 画面变化 → dy 恒定 → 控制量一路推到 ±45° 顶死。虽然物理上不动，
    // 但每帧仍在发指令，白白占串口带宽和舵机寿命，且 HUD 上一直显示异常值。
    if (!c.enable_tilt) d_tilt = 0.f;
    if (!c.enable_pan)  d_pan  = 0.f;    // 同理，pan 缸没接时也要关

    // 已经顶在限位、且还要继续往外推 → 不动。
    // 不加这条会看到角度停在 90° 但每帧仍下发指令，长期堵转。
    if ((pan >= c.pan_range - 1e-4f && d_pan > 0) ||
        (pan <= -c.pan_range + 1e-4f && d_pan < 0)) d_pan = 0.f;
    if ((tilt >= c.tilt_range - 1e-4f && d_tilt > 0) ||
        (tilt <= -c.tilt_range + 1e-4f && d_tilt < 0)) d_tilt = 0.f;

    out_pan = clampf(pan + d_pan, -c.pan_range, c.pan_range);
    out_tilt = clampf(tilt + d_tilt, -c.tilt_range, c.tilt_range);
    return false;
}

// ------------------------------------------------------------- 原有工具函数 --
static const cv::Scalar PALETTE[] = {
    cv::Scalar(255, 160, 40), cv::Scalar(0, 220, 255), cv::Scalar(80, 240, 100),
    cv::Scalar(255, 60, 240), cv::Scalar(240, 240, 40), cv::Scalar(0, 180, 255)
};

static bool is_traffic_class(int id) {
    return (id == 0 || id == 1 || id == 2 || id == 3 || id == 5 || id == 7);
}

static void draw_3d_box(cv::Mat& img, const Box3D& box, const cv::Scalar& color, int thickness = 2) {
    if (box.corners_2d.size() != 8) return;
    const auto& c = box.corners_2d;
    for (int i = 0; i < 4; ++i) {
        cv::line(img, cv::Point((int)c[i].u, (int)c[i].v),
                 cv::Point((int)c[(i + 1) % 4].u, (int)c[(i + 1) % 4].v), color, thickness);
    }
    for (int i = 0; i < 4; ++i) {
        cv::line(img, cv::Point((int)c[i + 4].u, (int)c[i + 4].v),
                 cv::Point((int)c[((i + 1) % 4) + 4].u, (int)c[((i + 1) % 4) + 4].v), color, thickness);
    }
    for (int i = 0; i < 4; ++i) {
        cv::line(img, cv::Point((int)c[i].u, (int)c[i].v),
                 cv::Point((int)c[i + 4].u, (int)c[i + 4].v), color, thickness);
    }
    cv::Scalar front_color(255, 128, 0);
    cv::line(img, cv::Point((int)c[0].u, (int)c[0].v), cv::Point((int)c[5].u, (int)c[5].v), front_color, 1);
    cv::line(img, cv::Point((int)c[1].u, (int)c[1].v), cv::Point((int)c[4].u, (int)c[4].v), front_color, 1);

    char dist_str[32];
    snprintf(dist_str, sizeof(dist_str), "%.1fm", box.distance_m);
    int tx = (int)c[4].u;
    int ty = std::max(18, (int)c[4].v - 6);
    int baseline = 0;
    cv::Size tsz = cv::getTextSize(dist_str, cv::FONT_HERSHEY_SIMPLEX, 0.45, 1, &baseline);
    cv::rectangle(img, cv::Rect(tx - 2, ty - tsz.height - 2, tsz.width + 4, tsz.height + 4),
                  cv::Scalar(18, 22, 26), -1);
    cv::rectangle(img, cv::Rect(tx - 2, ty - tsz.height - 2, tsz.width + 4, tsz.height + 4), color, 1);
    cv::putText(img, dist_str, cv::Point(tx, ty), cv::FONT_HERSHEY_SIMPLEX, 0.45,
                cv::Scalar(0, 255, 255), 1, cv::LINE_AA);
}

static cv::Mat render_bev_radar(int radar_size, float range_fwd, float range_lat,
                                const std::vector<std::pair<std::string, Box3D>>& objects) {
    cv::Mat canvas(radar_size, radar_size, CV_8UC3, cv::Scalar(20, 24, 28));
    int ego_u = radar_size / 2;
    int ego_v = radar_size - 25;
    float scale_fwd = (ego_v - 20) / range_fwd;
    float scale_lat = (radar_size / 2.0f - 15) / range_lat;

    auto v2c = [&](float X_v, float Y_v) -> cv::Point {
        return cv::Point((int)std::round(ego_u - Y_v * scale_lat),
                         (int)std::round(ego_v - X_v * scale_fwd));
    };

    cv::Scalar ring_color(45, 55, 65), text_color(120, 140, 160);
    for (float dist_m : {10.0f, 20.0f, 30.0f, 40.0f, 50.0f}) {
        if (dist_m <= range_fwd) {
            int radius_px = (int)std::round(dist_m * scale_fwd);
            cv::circle(canvas, cv::Point(ego_u, ego_v), radius_px, ring_color, 1, cv::LINE_AA);
            char lbl[16];
            snprintf(lbl, sizeof(lbl), "%dm", (int)dist_m);
            cv::putText(canvas, lbl, cv::Point(ego_u + 6, ego_v - radius_px + 12),
                        cv::FONT_HERSHEY_SIMPLEX, 0.32, text_color, 1, cv::LINE_AA);
        }
    }
    for (float lat_m : {-10.0f, -5.0f, 0.0f, 5.0f, 10.0f}) {
        cv::Point p = v2c(0.0f, lat_m);
        if (p.x >= 10 && p.x < radar_size - 10) {
            cv::Scalar col = (lat_m == 0.0f) ? cv::Scalar(50, 65, 80) : cv::Scalar(35, 42, 50);
            cv::line(canvas, cv::Point(p.x, 20), cv::Point(p.x, ego_v), col,
                     (lat_m == 0.0f) ? 2 : 1);
        }
    }
    float fov_rad = 32.5f * 3.14159265f / 180.0f;
    float max_y = range_fwd * std::tan(fov_rad);
    cv::line(canvas, cv::Point(ego_u, ego_v), v2c(range_fwd, max_y), cv::Scalar(40, 70, 90), 1, cv::LINE_AA);
    cv::line(canvas, cv::Point(ego_u, ego_v), v2c(range_fwd, -max_y), cv::Scalar(40, 70, 90), 1, cv::LINE_AA);

    std::vector<cv::Point> ego_pts = {
        {ego_u, ego_v - 12}, {ego_u - 7, ego_v + 6}, {ego_u, ego_v + 2}, {ego_u + 7, ego_v + 6}
    };
    cv::fillPoly(canvas, std::vector<std::vector<cv::Point>>{ego_pts}, cv::Scalar(0, 220, 255));
    cv::putText(canvas, "BEV RADAR (ISO Ego)", cv::Point(12, 18),
                cv::FONT_HERSHEY_SIMPLEX, 0.40, cv::Scalar(0, 255, 200), 1, cv::LINE_AA);

    int idx = 0;
    for (const auto& item : objects) {
        const Box3D& b = item.second;
        if (b.longitudinal_m < 0.5f || b.longitudinal_m > range_fwd) continue;
        cv::Point c = v2c(b.longitudinal_m, b.lateral_m);
        if (c.x < 0 || c.x >= radar_size || c.y < 0 || c.y >= radar_size) continue;
        int half_w = std::max(2, (int)std::round((b.dims.y / 2.0f) * scale_lat));
        int half_l = std::max(3, (int)std::round((b.dims.x / 2.0f) * scale_fwd));
        cv::Scalar fill_col(255, 160, 40), edge_col(255, 220, 100);
        if (item.first == "person") { fill_col = cv::Scalar(40, 220, 255); edge_col = cv::Scalar(100, 255, 255); }
        cv::rectangle(canvas, cv::Rect(c.x - half_w, c.y - half_l, half_w * 2, half_l * 2), fill_col, -1);
        cv::rectangle(canvas, cv::Rect(c.x - half_w, c.y - half_l, half_w * 2, half_l * 2), edge_col, 1);
        char tag[32];
        snprintf(tag, sizeof(tag), "#%d:%.1fm", ++idx, b.distance_m);
        cv::putText(canvas, tag, cv::Point(c.x + half_w + 3, c.y + 4),
                    cv::FONT_HERSHEY_SIMPLEX, 0.38, cv::Scalar(255, 255, 255), 1, cv::LINE_AA);
    }
    return canvas;
}

#define HTTP_PORT 8080
#define MODEL_PATH "/root/edgevision/data/models/yolov8n_dflfree_rk3588_i8.rknn"

static const char* COCO80[] = {
    "person","bicycle","car","motorcycle","airplane","bus","train","truck","boat","traffic light",
    "fire hydrant","stop sign","parking meter","bench","bird","cat","dog","horse","sheep","cow",
    "elephant","bear","zebra","giraffe","backpack","umbrella","handbag","tie","suitcase","frisbee",
    "skis","snowboard","sports ball","kite","baseball bat","baseball glove","skateboard","surfboard",
    "tennis racket","bottle","wine glass","cup","fork","knife","spoon","bowl","banana","apple",
    "sandwich","orange","broccoli","carrot","hot dog","pizza","donut","cake","chair","couch",
    "potted plant","bed","dining table","toilet","tv","laptop","mouse","remote","keyboard","cell phone",
    "microwave","oven","toaster","sink","refrigerator","book","clock","vase","scissors","teddy bear",
    "hair drier","toothbrush"
};

static std::vector<uint8_t> g_latest_jpeg;
static pthread_mutex_t g_jpeg_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_jpeg_cond = PTHREAD_COND_INITIALIZER;
static std::vector<char> g_model_data;

static void handle_signal(int) { g_running = 0; }

static bool load_model_file(const char* path) {
    FILE* fp = fopen(path, "rb");
    if (!fp) { perror("fopen model"); return false; }
    fseek(fp, 0, SEEK_END);
    long sz = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    g_model_data.resize((size_t)sz);
    bool ok = fread(g_model_data.data(), 1, (size_t)sz, fp) == (size_t)sz;
    fclose(fp);
    printf("[+] 模型 %s (%.2f MB)\n", path, sz / 1e6);
    return ok;
}

static void letterbox(const cv::Mat& src, cv::Mat& dst, int size, float& scale, int& pad_x, int& pad_y) {
    const int w = src.cols, h = src.rows;
    scale = std::min((float)size / w, (float)size / h);
    const int nw = (int)(w * scale), nh = (int)(h * scale);
    cv::Mat resized;
    cv::resize(src, resized, cv::Size(nw, nh), 0, 0, cv::INTER_LINEAR);
    dst = cv::Mat(size, size, src.type(), cv::Scalar(114, 114, 114));
    pad_x = (size - nw) / 2;
    pad_y = (size - nh) / 2;
    resized.copyTo(dst(cv::Rect(pad_x, pad_y, nw, nh)));
}

static void send_all(int fd, const char* data, size_t len) {
    size_t off = 0;
    while (off < len) {
        ssize_t n = send(fd, data + off, len - off, MSG_NOSIGNAL);
        if (n <= 0) return;
        off += (size_t)n;
    }
}

// ---- 网页（比原版多了云台状态面板）----
static const char* kIndexHtml = R"HTML(<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EdgeVision RK3588 · 云台追踪实时流</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0b1120;color:#e2e8f0;font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header{padding:14px 22px;border-bottom:1px solid #1e293b;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
h1{font-size:15px;margin:0;font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;background:#64748b}
.dot.on{background:#22c55e;box-shadow:0 0 8px #22c55e}
.sub{font-size:12px;color:#94a3b8;margin-left:auto;font-variant-numeric:tabular-nums}
main{padding:16px 22px;display:flex;justify-content:center;gap:16px;align-items:flex-start;flex-wrap:wrap}
.frame{max-width:100%;border:1px solid #1e293b;border-radius:10px;overflow:hidden;background:#000;line-height:0}
img{display:block;max-width:100%;height:auto}
.panel{min-width:230px;border:1px solid #1e293b;border-radius:10px;padding:14px 16px;background:#0f172a}
.panel h2{font-size:12px;margin:0 0 10px;color:#94a3b8;font-weight:600;letter-spacing:.5px;text-transform:uppercase}
.row{display:flex;justify-content:space-between;font-size:13px;padding:5px 0;font-variant-numeric:tabular-nums}
.row span:first-child{color:#94a3b8}
.row b{color:#38bdf8;font-weight:600}
.row b.ok{color:#22c55e}
.row b.warn{color:#f59e0b}
.bar{height:5px;background:#1e293b;border-radius:3px;margin-top:3px;overflow:hidden}
.bar i{display:block;height:100%;background:#38bdf8;transition:width .12s linear}
footer{padding:0 22px 22px;font-size:12px;color:#64748b;line-height:1.9}
a{color:#7dd3fc;text-decoration:none}a:hover{text-decoration:underline}
</style></head>
<body>
<header><span class="dot" id="dot"></span><h1>EdgeVision RK3588 · 云台追踪实时流</h1><span class="sub" id="st">连接中…</span></header>
<main>
  <div class="frame"><img id="v" src="/stream" alt="live"></div>
  <div class="panel">
    <h2>云台状态</h2>
    <div class="row"><span>Pan</span><b id="pan">-</b></div>
    <div class="bar"><i id="panbar" style="width:50%"></i></div>
    <div class="row"><span>Tilt</span><b id="tilt">-</b></div>
    <div class="bar"><i id="tiltbar" style="width:50%"></i></div>
    <div class="row"><span>锁定</span><b id="lock">-</b></div>
    <div class="row"><span>下发</span><b id="sent">-</b></div>
    <div class="row"><span>串口错误</span><b id="err">-</b></div>
    <h2 style="margin-top:16px">检测</h2>
    <div class="row"><span>目标数</span><b id="objs">-</b></div>
    <div class="row"><span>NPU 推理</span><b id="npu">-</b></div>
    <div class="row"><span>帧率</span><b id="fps">-</b></div>
    <h2 style="margin-top:16px">采集健康度</h2>
    <div class="row"><span>断帧累计</span><b id="drop">-</b></div>
    <div class="row"><span>重开设备</span><b id="reopen">-</b></div>
    <div class="row"><span>编码失败</span><b id="badjpg">-</b></div>
  </div>
</main>
<footer>
取流 → letterbox → NPU 三核推理 → DFL 解码 → 3D 反投影 → 串口驱动云台 → MJPEG 推流<br>
接口：<a href="/">/</a> 页面 ｜ <a href="/stream">/stream</a> 流 ｜ <a href="/snapshot">/snapshot</a> 快照 ｜ <a href="/status">/status</a> JSON
</footer>
<script>
// 状态轮询。为什么用 JSON 而不是把数字画进画面：
//   画进画面会挡住检测框，而且改一次要重编译 C++；走 JSON 改前端零成本。
async function tick(){
  try{
    const r=await fetch('/status',{cache:'no-store'});
    const s=await r.json();
    document.getElementById('pan').textContent=s.pan.toFixed(1)+'°';
    document.getElementById('tilt').textContent=s.tilt.toFixed(1)+'°';
    // 用进度条直观表示角度：±90 / ±45 映射到 0~100%
    document.getElementById('panbar').style.width=((s.pan+90)/180*100).toFixed(1)+'%';
    document.getElementById('tiltbar').style.width=((s.tilt+45)/90*100).toFixed(1)+'%';
    const lock=document.getElementById('lock');
    lock.textContent=s.locked?'居中':'追踪中';
    lock.className='ok'; if(!s.locked) lock.className='warn';
    document.getElementById('sent').textContent=s.sent;
    const e=document.getElementById('err');
    e.textContent=s.errors; e.className=s.errors>0?'warn':'ok';
    document.getElementById('objs').textContent=s.objects;
    document.getElementById('npu').textContent=s.npu_ms.toFixed(1)+' ms';
    document.getElementById('fps').textContent=s.fps.toFixed(1);
    // 断帧一定要显示：>0 说明 USB 链路/带宽有问题，画面看着正常也可能在丢帧
    const dp=document.getElementById('drop');
    dp.textContent=s.dropped;
    dp.className=(s.dropped>0)?'warn':'ok';
    document.getElementById('reopen').textContent=s.reopens;
    const bj=document.getElementById('badjpg');
    bj.textContent=s.badjpeg;
    bj.className=(s.badjpeg>0)?'warn':'ok';
    document.getElementById('st').textContent='LIVE';
    document.getElementById('dot').className='dot on';
  }catch(err){
    document.getElementById('st').textContent='状态获取失败';
    document.getElementById('dot').className='dot';
  }
}
setInterval(tick,500); tick();
var img=document.getElementById('v');
img.onerror=function(){setTimeout(function(){img.src='/stream?t='+Date.now();},2000);};
</script>
</body></html>)HTML";

// ---- 全局状态（给 /status 用）----
struct LiveStatus {
    pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
    float pan = 0.f, tilt = 0.f;
    bool locked = false;
    int sent = 0, errors = 0, objects = 0;
    double npu_ms = 0.0, fps = 0.0;
    // 采集健康度：断帧计数与重开次数必须能被网页看到，
    // 否则"画面在动"会掩盖"每 30 帧丢 1 帧"这种慢性问题（AGENTS.md 第①条）。
    unsigned long long dropped = 0;
    unsigned long long reopens = 0;
    unsigned long long badjpeg = 0;
};
static LiveStatus g_status;

static void serve_index(int fd) {
    char hdr[256];
    const size_t len = strlen(kIndexHtml);
    int n = snprintf(hdr, sizeof(hdr),
        "HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
        "Content-Length: %zu\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n", len);
    send_all(fd, hdr, n);
    send_all(fd, kIndexHtml, len);
}

static void serve_snapshot(int fd) {
    std::vector<uint8_t> jpg;
    pthread_mutex_lock(&g_jpeg_lock);
    jpg = g_latest_jpeg;
    pthread_mutex_unlock(&g_jpeg_lock);
    if (jpg.empty()) {
        const char* r = "HTTP/1.0 503 No Frame\r\nContent-Length: 0\r\n\r\n";
        send_all(fd, r, strlen(r));
        return;
    }
    char hdr[256];
    int n = snprintf(hdr, sizeof(hdr),
        "HTTP/1.0 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: %zu\r\n"
        "Cache-Control: no-cache\r\nConnection: close\r\n\r\n", jpg.size());
    send_all(fd, hdr, n);
    send_all(fd, (const char*)jpg.data(), jpg.size());
}

static void serve_status(int fd) {
    pthread_mutex_lock(&g_status.lock);
    char body[768];   // 加 dropped/reopens/badjpeg 三个 %llu 后仍留足余量（最长约 300B）
    int len = snprintf(body, sizeof(body),
        "{\"pan\":%.2f,\"tilt\":%.2f,\"locked\":%s,\"sent\":%d,\"errors\":%d,"
        "\"objects\":%d,\"npu_ms\":%.2f,\"fps\":%.2f,"
        "\"dropped\":%llu,\"reopens\":%llu,\"badjpeg\":%llu}",
        g_status.pan, g_status.tilt, g_status.locked ? "true" : "false",
        g_status.sent, g_status.errors, g_status.objects,
        g_status.npu_ms, g_status.fps, g_status.dropped, g_status.reopens,
        g_status.badjpeg);
    pthread_mutex_unlock(&g_status.lock);

    char hdr[220];
    int n = snprintf(hdr, sizeof(hdr),
        "HTTP/1.0 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
        "Content-Length: %d\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n", len);
    send_all(fd, hdr, n);
    send_all(fd, body, len);
}

static void* http_server_thread(void*) {
    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    int opt = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(HTTP_PORT);
    if (bind(server_fd, (sockaddr*)&addr, sizeof(addr)) < 0) { perror("bind"); return nullptr; }
    listen(server_fd, 8);
    printf("[+] 网页预览: http://<board-ip>:%d   ( / 页面 | /stream 流 | /snapshot | /status )\n", HTTP_PORT);

    while (g_running) {
        int c_fd = accept(server_fd, nullptr, nullptr);
        if (c_fd < 0) break;

        char req[1024] = {0};
        recv(c_fd, req, sizeof(req) - 1, 0);

        char path[128] = "/";
        if (sscanf(req, "GET %127s", path) != 1) { close(c_fd); continue; }

        if (strncmp(path, "/snapshot", 9) == 0) { serve_snapshot(c_fd); close(c_fd); continue; }
        if (strncmp(path, "/status", 7) == 0)   { serve_status(c_fd);   close(c_fd); continue; }
        if (strncmp(path, "/stream", 7) != 0)   { serve_index(c_fd);    close(c_fd); continue; }

        const char* stream_header =
            "HTTP/1.0 200 OK\r\n"
            "Server: RK3588-EdgeVision\r\n"
            "Connection: close\r\n"
            "Cache-Control: no-cache, no-store, must-revalidate\r\n"
            "Pragma: no-cache\r\n"
            "Content-Type: multipart/x-mixed-replace; boundary=--frame\r\n\r\n";
        send_all(c_fd, stream_header, strlen(stream_header));

        while (g_running) {
            std::vector<uint8_t> jpeg_copy;
            pthread_mutex_lock(&g_jpeg_lock);
            pthread_cond_wait(&g_jpeg_cond, &g_jpeg_lock);
            if (!g_running) { pthread_mutex_unlock(&g_jpeg_lock); break; }
            jpeg_copy = g_latest_jpeg;
            pthread_mutex_unlock(&g_jpeg_lock);
            if (jpeg_copy.empty()) continue;

            char part_hdr[128];
            int hdr_len = snprintf(part_hdr, sizeof(part_hdr),
                "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %zu\r\n\r\n", jpeg_copy.size());
            if (send(c_fd, part_hdr, hdr_len, MSG_NOSIGNAL) < 0) break;
            if (send(c_fd, jpeg_copy.data(), jpeg_copy.size(), MSG_NOSIGNAL) < 0) break;
            if (send(c_fd, "\r\n", 2, MSG_NOSIGNAL) < 0) break;
        }
        close(c_fd);
    }
    close(server_fd);
    return nullptr;
}

// ============================================================================
//  摄像头采集封装 —— 抗 MJPEG 断帧（2026-11-11 线上崩了两次，本节是修复）
//
//  现象（板端原样复现）
//  -------------------
//      [profile] ... LOST | 拒绝框 0
//      terminate called after throwing an instance of 'cv::Exception'
//        what(): OpenCV(4.5.1) ../modules/imgcodecs/src/loadsave.cpp:768:
//                  error: (-215:Assertion failed) !buf.empty() in function 'imdecode_'
//      Aborted
//
//  根因：抛异常的不是我们的代码，是 OpenCV 的 V4L2 后端在替我们解 MJPEG。
//    cap_v4l.cpp(4.5.1) 第 1584 行，convertToRgb() 的 MJPEG 分支：
//        cv::imdecode(Mat(1, currentBuffer.buffer.bytesused, CV_8U, start), ...);
//    ★ 它把 bytesused 直接当 Mat 的列数用，**不判 0、不查 V4L2_BUF_FLAG_ERROR、
//      也不看 imdecode 返回值**（4.5.1 全文搜不到 V4L2_BUF_FLAG_ERROR，4.x 到
//      现在仍是这一行，没修）。
//    于是 bytesused==0 时 Mat 是空的 → imdecode_ 首行 CV_Assert(!buf.empty())
//    （loadsave.cpp:768，和报错行号完全对得上）抛 cv::Exception → 循环没
//    try/catch → 一路到 std::terminate → Aborted。
//
//    ★ 为什么"截断的 JPEG"不是元凶（这条别搞错，会带偏排查方向）：
//      Mat(1, N, CV_8U, ptr) 的 total()==N，所以只要 N>0，!buf.empty() 就通过，
//      后面 findDecoder 失败只是返回 false（不出图），**不会抛这个断言**。
//      即：能产生 `-215 !buf.empty()` 的只有 N==0，也就是**零长度载荷**。
//
//    驱动什么时候会给零长度？(以下为推测，未在板端拿 OPENCV_LOG_LEVEL=DEBUG 证实)
//      USB 等时带宽吃紧 / AE 弱光下拉长曝光 / 云台舵机与摄像头同线束抢供电，
//      都可能让 uvcvideo 回收坏 buffer 并把 bytesused 归零。
//      验证方法见 docs/TROUBLESHOOTING.md 第 53 条。
//
//  关键认识：`if (bgr.empty()) continue;` 挡不住这件事。
//    异常在 `cap >> bgr` 内部就抛出了，赋值语句都没执行完，
//    所以 bgr.empty() 那行代码永远等不到这次坏帧。这是本 bug 最容易被
//    "我明明判了空"骗过去的地方。
//
//  处理原则（和 AGENTS.md 第①条一致）：断帧必须【计数 + 上报】。
//    悄悄 catch 掉 = 谎报帧率，所以每个坏帧都累加 g_cam_dropped，
//    并出现在 [profile] 行、/status JSON 和网页面板上。
// ============================================================================
static uint64_t g_cam_dropped     = 0;   // 累计断帧（解不出来的帧）
static uint64_t g_cam_reopens     = 0;   // 累计重开设备次数
static uint64_t g_cam_badjpeg     = 0;   // 累计编码失败帧（推送沿用上一张）
static int      g_cam_consec_fail = 0;   // 连续坏帧数（成功一帧就清零）

// 连续失败到这个数，就认为设备/USB 管道已经不可信，重开一次。
// 取 8 的理由（有源码依据，不是拍的）：
//   retrieveFrame() 里 convertToRgb() 抛异常时，它后面的
//       tryIoctl(VIDIOC_QBUF) / bufferIndex = -1
//   两句都没执行到 → 那个 buffer 漏还、bufferIndex 卡在 >=0。
//   但下一次 grabFrame() 开头就是
//       if (bufferIndex >= 0) { tryIoctl(VIDIOC_QBUF, ...) }
//   **自己会把漏还的 buffer 补还队列** → 单次异常后对象仍可用，不会一直抛。
//   所以：偶发单帧断帧 catch + continue 就够了，不必重开；
//         只有"连续 8 帧都读不出来"才说明不是偶发坏包，而是管道/带宽层面
//         已经坏了，这时继续读只会无限抛，重开设备才是正解。
static const int CAM_REOPEN_AFTER = 8;

// 打开（或重开）摄像头。参数 is_video_file=true 时按文件打开（离线回放）。
// verbose=false 用于重开时压噪音。
static bool open_camera(cv::VideoCapture& cap, const char* dev_path,
                        bool is_video_file, bool verbose = true) {
    cap.release();
    if (is_video_file) {
        cap.open(dev_path);
        if (!cap.isOpened()) {
            std::cerr << "[-] 无法打开视频文件: " << dev_path << "\n";
            return false;
        }
        if (verbose) printf("[+] 已打开视频文件: %s\n", dev_path);
        return true;
    }

    cap.open(dev_path, cv::CAP_V4L2);
    if (!cap.isOpened()) {
        std::cerr << "[-] 无法打开摄像头: " << dev_path << "\n";
        return false;
    }
    cap.set(cv::CAP_PROP_FOURCC, cv::VideoWriter::fourcc('M', 'J', 'P', 'G'));
    cap.set(cv::CAP_PROP_FRAME_WIDTH, 1280);
    cap.set(cv::CAP_PROP_FRAME_HEIGHT, 720);
    cap.set(cv::CAP_PROP_FPS, 30);

    // 和 lab/vision/edgevision_gateway.cpp:140 保持一致：关掉 exposure_auto_priority，
    // 打破 V4L2 默认的 20FPS 锁帧（网关那边是实测过的）。
    // 注意：这一句**不是**断帧的修复，它只是让采流更稳。
    // 断帧的真正修复是下面的 try/catch（见本节顶部注释）。
    {
        char cmd[160];
        snprintf(cmd, sizeof(cmd),
                 "v4l2-ctl -d %s --set-ctrl=exposure_auto_priority=0 >/dev/null 2>&1",
                 dev_path);
        if (system(cmd) != 0) { /* 没有 v4l2-ctl 也不影响主流程 */ }
    }

    // ★★★ 不要在这里设 CAP_PROP_BUFFERSIZE ★★★
    //
    // 2026-11-11 现场踩到了：加了这一行之后，程序在预热阶段一帧都读不到，
    // 一直刷 `select() timeout`，8080 因此也起不来。
    //
    // 源码依据（OpenCV 4.5.1 cap_v4l.cpp）：
    //   CAP_PROP_BUFFERSIZE 的 setter（L1999-2008）不是"记个数字"那么简单，
    //   只要新值和旧值不同，它就 `return v4l2_reset();`，而
    //       bool v4l2_reset() { streaming(false); releaseBuffers(); return initCapture(); }
    //   也就是 **停流 → 释放全部 mmap 缓冲 → 重新协商格式 → 重新申请缓冲**。
    //
    //   默认值 DEFAULT_V4L_BUFFERS = 4（L284），我们传 1（L2007 要求 1..10 合法），
    //   所以必然触发这一次完整重置 —— 而我们刚 open() 完，驱动还没出第一帧，
    //   这一重置把正在起的流打回去，设备就再也吐不出帧。
    //
    //   教训：**"顺手优化"也要先看它在库里到底做了什么。**
    //   这一行的收益（少 3 个缓冲、少点延迟）远小于风险，直接去掉，
    //   让 OpenCV 用它自己的默认值。

    // AE 预热：丢前 15 帧等自动曝光收敛。
    // ★ 这一步同样在解 MJPEG，同样会抛异常 —— 必须也包 try/catch。
    //   不包的话，"启动即崩"和"跑两秒才崩"看起来像两个 bug，白查半天。
    //
    // ★ 另：这里必须有"读不到就早退"的出口。
    //   OpenCV 的 select 默认超时 10 秒/次（OPENCV_VIDEOIO_V4L_SELECT_TIMEOUT），
    //   原来那版 15 次全失败会卡 150 秒 —— 而 8080 的 HTTP 线程是在
    //   摄像头打开之后才启动的，于是"网页打不开"变成了第二个症状。
    //   现在改成：连续 3 帧没有任何进展就放弃预热，带着计数继续往下跑，
    //   让 HTTP/9000 先起来，坏帧交给主循环的 cam_read() 慢慢恢复。
    cv::Mat warm;
    int warm_ok = 0;
    for (int i = 0; i < 15; ++i) {
        try {
            cap >> warm;
            if (!warm.empty()) warm_ok++;
        } catch (const cv::Exception& e) {
            g_cam_dropped++;
            std::cerr << "[!] 预热阶段断帧（已计数）: " << e.what() << "\n";
        }
        if (i >= 2 && warm_ok == 0) {
            std::cerr << "[!] 预热连 3 帧无进展，跳过预热先让服务起来\n"
                         "    （若之后主循环仍一直拿到空帧，说明 /dev/video81 被别的"
                         "进程占着或已卡死：\n"
                         "     先 fuser -v /dev/video81 看占用者；\n"
                         "     再 bash /root/esp32_gimbal/uvc_recover.sh 复位；\n"
                         "     详见 docs/TROUBLESHOOTING.md 第 54 条）\n";
            break;
        }
    }
    if (verbose) {
        printf("[+] 已打开摄像头: %s @ 1280x720 30FPS（预热拿到 %d/15 帧）\n",
               dev_path, warm_ok);
    }
    return true;
}

// 读一帧。返回 false = 这一帧坏了（已计数），调用方 continue 即可。
// 连续坏帧到 CAM_REOPEN_AFTER 时由调用方重开设备。
static bool cam_read(cv::VideoCapture& cap, cv::Mat& dst) {
    // ★ 必须先清空 dst。
    //   OpenCV 的 VideoCapture::read() 在失败时**不会改**输出 Mat。
    //   如果这里不 release，上一帧就会原样留在 dst 里被当成新帧继续用：
    //   表现是"帧率虚高 / 画面卡住但程序看着还在跑"，比崩溃更难查。
    dst.release();

    bool ok = false;
    try {
        ok = cap.read(dst);        // read() == grab() + retrieve()
    } catch (const cv::Exception& e) {
        g_cam_dropped++;
        g_cam_consec_fail++;
        // 断帧会刷屏：前 5 次每次打，之后每 60 次打一行；计数永远准
        if (g_cam_dropped <= 5 || (g_cam_dropped % 60) == 0) {
            std::cerr << "[!] 断帧 #" << g_cam_dropped
                      << "（累计重开 " << g_cam_reopens << "）: " << e.what() << "\n";
        }
        return false;
    }

    // 不抛异常但没拿到图：select 超时 / 驱动还没出帧 / 文件 EOF
    if (!ok || dst.empty()) {
        g_cam_dropped++;
        g_cam_consec_fail++;
        return false;
    }
    g_cam_consec_fail = 0;
    return true;
}

// ------------------------------------------------------------------ main --
int main(int argc, char* argv[]) {
    // ★ 把 V4L2 的 select 超时从默认 10 秒压到 3 秒（必须在任何 VideoCapture 之前设）。
    //
    // 为什么：cap_v4l.cpp:993 是
    //     static int param_v4l_select_timeout =
    //         getConfigurationParameterSizeT("OPENCV_VIDEOIO_V4L_SELECT_TIMEOUT", 10);
    // 默认 10 秒是**每次** select 的超时。摄像头卡死时，预热 15 次 × 10 秒 = 卡 150 秒，
    // 用户看到的就是"程序没反应、网页打不开"。压到 3 秒后最坏 15×3 ≈ 45 秒，
    // 加上"连续 3 帧无进展就早退"，实际卡住时间降到 ~9 秒以内。
    //
    // 用 setenv 的注意点：只有在 OpenCV 第一次执行到这行 static 初始化时**之前**设才有效。
    // 这里在 main 开头设，早于任何 VideoCapture 构造，满足条件。
    // 如果用户已经显式设过，尊重用户的值（不自作主张覆盖）。
    setenv("OPENCV_VIDEOIO_V4L_SELECT_TIMEOUT", "3", 0);

    const char* dev_path = "/dev/video81";
    const char* gimbal_dev = nullptr;
    if (argc > 1 && argv[1][0] != '-') dev_path = argv[1];

    ControlCfg cfg;
    const char* bridge_host = nullptr;
    int bridge_port = 9100;
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--gimbal") == 0 && i + 1 < argc) gimbal_dev = argv[++i];
        // --bridge 走 TCP 桥接（pyusb 控 USB CDC），不需要 /dev/tty*
        else if (strcmp(argv[i], "--bridge") == 0 && i + 1 < argc) {
            // 支持 "127.0.0.1" 或 "127.0.0.1:9100"
            static char hostbuf[128];
            const char* a = argv[++i];
            const char* colon = strrchr(a, ':');
            if (colon) {
                size_t hl = (size_t)(colon - a);
                if (hl >= sizeof(hostbuf)) hl = sizeof(hostbuf) - 1;
                memcpy(hostbuf, a, hl);
                hostbuf[hl] = 0;
                bridge_host = hostbuf;
                bridge_port = atoi(colon + 1);
            } else {
                snprintf(hostbuf, sizeof(hostbuf), "%s", a);
                bridge_host = hostbuf;
            }
        }
        else if (strcmp(argv[i], "--bridge-port") == 0 && i + 1 < argc) bridge_port = atoi(argv[++i]);
        else if (strcmp(argv[i], "--kp-pan") == 0 && i + 1 < argc) cfg.kp_pan = atof(argv[++i]);
        else if (strcmp(argv[i], "--kp-tilt") == 0 && i + 1 < argc) cfg.kp_tilt = atof(argv[++i]);
        else if (strcmp(argv[i], "--max-step") == 0 && i + 1 < argc) cfg.max_step = atof(argv[++i]);
        else if (strcmp(argv[i], "--deadzone") == 0 && i + 1 < argc) cfg.deadzone = atof(argv[++i]);
        else if (strcmp(argv[i], "--min-score") == 0 && i + 1 < argc) cfg.min_score = atof(argv[++i]);
        else if (strcmp(argv[i], "--no-invert-pan") == 0) cfg.invert_pan = false;
        else if (strcmp(argv[i], "--no-invert-tilt") == 0) cfg.invert_tilt = false;
        else if (strcmp(argv[i], "--no-pan") == 0) cfg.enable_pan = false;
        else if (strcmp(argv[i], "--no-tilt") == 0) cfg.enable_tilt = false;
        else if (strcmp(argv[i], "--smooth") == 0 && i + 1 < argc) cfg.smooth_alpha = atof(argv[++i]);
        else if (strcmp(argv[i], "--jump-max") == 0 && i + 1 < argc) cfg.jump_max = atof(argv[++i]);
        else if (strcmp(argv[i], "--no-target-filter") == 0) cfg.filter_partial = false;
        else if (strcmp(argv[i], "--settle") == 0 && i + 1 < argc) cfg.settle_frames = atoi(argv[++i]);
        else if (strcmp(argv[i], "--no-3d") == 0) cfg.show_3d = false;
    }

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);
    signal(SIGPIPE, SIG_IGN);

    if (!load_model_file(MODEL_PATH)) return 1;

    rknn_context ctx = 0;
    if (rknn_init(&ctx, g_model_data.data(), (uint32_t)g_model_data.size(), 0, nullptr) < 0) {
        std::cerr << "[-] rknn_init 失败\n";
        return 1;
    }
    rknn_set_core_mask(ctx, RKNN_NPU_CORE_0_1_2);

    rknn_input_output_num io{};
    rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io, sizeof(io));
    int bi = -1, ci = -1;
    bool bnhwc = false, cnhwc = false;
    for (uint32_t i = 0; i < io.n_output; ++i) {
        rknn_tensor_attr a{};
        a.index = i;
        rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &a, sizeof(a));
        for (int d = 0; d < a.n_dims; ++d) {
            if (a.dims[d] == 8400) {
                bool nhwc = (d != a.n_dims - 1);
                int ch = (int)(nhwc ? a.dims[a.n_dims - 1] : a.dims[a.n_dims - 2]);
                if (ch == 64) { bi = (int)i; bnhwc = nhwc; }
                else if (ch == 80) { ci = (int)i; cnhwc = nhwc; }
                break;
            }
        }
    }
    if (bi < 0 || ci < 0) { std::cerr << "[-] 输出识别失败\n"; return 1; }
    printf("[+] 输出: raw_box idx=%d(nhwc=%d)  cls_sig idx=%d(nhwc=%d)\n", bi, bnhwc, ci, cnhwc);

    // ---- 云台（可选）----
    // 两种通路：
    //   --gimbal /dev/ttyUSB0   直连串口（TTL 方案，需要 /dev/ttyUSB*）
    //   --bridge 127.0.0.1:9100 走 Python 桥接（pyusb + USB CDC，
    //                           绕开内核缺 CONFIG_USB_ACM 的限制）
    GimbalSerial gimbal;
    bool gimbal_on = false;
    if (bridge_host) {
        if (gimbal.open_tcp(bridge_host, bridge_port)) {
            gimbal_on = true;
            printf("[+] 云台控制已启用（TCP 桥接）  kp=(%.2f,%.2f) max_step=%.1f "
                   "invert=(pan=%d,tilt=%d) enable=(pan=%d,tilt=%d) min_score=%.2f\n",
                   cfg.kp_pan, cfg.kp_tilt, cfg.max_step,
                   (int)cfg.invert_pan, (int)cfg.invert_tilt,
                   (int)cfg.enable_pan, (int)cfg.enable_tilt, cfg.min_score);
        } else {
            std::cerr << "[!] 桥接服务连不上，退化为纯预览模式\n";
            std::cerr << "    先跑：/root/rknn_env/bin/python /root/esp32_gimbal/gimbal_bridge.py\n";
        }
    } else if (gimbal_dev) {
        if (gimbal.open(gimbal_dev)) {
            gimbal_on = true;
            printf("[+] 云台控制已启用  kp=(%.2f,%.2f) max_step=%.1f invert=(pan=%d,tilt=%d) "
                   "enable=(pan=%d,tilt=%d) min_score=%.2f\n",
                   cfg.kp_pan, cfg.kp_tilt, cfg.max_step,
                   (int)cfg.invert_pan, (int)cfg.invert_tilt,
                   (int)cfg.enable_pan, (int)cfg.enable_tilt, cfg.min_score);
        } else {
            std::cerr << "[!] 云台串口打不开，退化为纯预览模式\n";
        }
    } else {
        printf("[i] 未指定 --gimbal / --bridge，纯预览模式（不驱动云台）\n");
    }

    // ---- 先把两个服务起起来，再去碰摄像头 ----
    //
    // ★ 顺序很关键（2026-11-11 现场踩到）：原来是先 open_camera() 再建线程。
    //   摄像头一旦卡在预热（select() 超时 10 秒/次 × 15），8080 就永远不 bind，
    //   用户看到的是"网页打不开"——一个和摄像头毫无关系的第二症状，
    //   会把人往"端口被占 / 防火墙"方向带偏。
    //
    //   服务先起的好处：摄像头再慢/再坏，页面都打得开，
    //   /status 能读到 dropped 计数，/snapshot 返回 503 No Frame，
    //   一眼就知道"服务活着、是相机没出图"，而不是"什么都连不上"。
    pthread_t http_tid;
    pthread_create(&http_tid, nullptr, http_server_thread, nullptr);

    // 9000 检测流：与 gateway 协议一致，让 Agent / 既有客户端照常能连
    pthread_t det_tid;
    pthread_create(&det_tid, nullptr, detect_streamer_thread, nullptr);

    // ---- 摄像头 ----
    // 打开/预热都封在 open_camera() 里，内部对 MJPEG 断帧做了 try/catch 与计数。
    bool is_video_file = (strncmp(dev_path, "/dev/video", 10) != 0);
    cv::VideoCapture cap;
    if (!open_camera(cap, dev_path, is_video_file)) {
        return 1;   // 注意：这里退化为"开不了就退出"，因为它连设备都打不开，
                    // 和"能开但偶尔断帧"是两回事，后者由 cam_read() 兜住。
    }

    std::vector<rknn_output> outputs(io.n_output);
    cv::Mat bgr, lb, rgb;
    std::vector<Detection> dets;
    uint32_t frame_id = 0;
    auto t_start = std::chrono::steady_clock::now();

    // 云台当前角度（跟踪我们**最后一次确认**的角度，不是想要的角度）
    float cur_pan = 0.f, cur_tilt = 0.f;
    bool locked = false;
    int lost_frames = 0;

    // 低通滤波状态 + 收敛后暂停
    float sm_cx = 0.5f, sm_cy = 0.5f;
    bool sm_init = false;
    int jump_skipped = 0;
    uint32_t settle_until_frame = 0;   // 在此之前不重新计算控制量

    while (g_running) {
        auto tA = std::chrono::steady_clock::now();
        // cam_read 已经把 cv::Exception 挡在循环里了。返回值 false 的情况：
        //   ① MJPEG 断帧（零长度载荷 → OpenCV 内部 imdecode 抛 !buf.empty()）→ 计数后跳帧
        //   ② 不抛异常但没等到图 → 也算一次断帧
        // 原来这里是裸的 `cap >> bgr;`，断帧即 Aborted。
        if (!cam_read(cap, bgr)) {
            if (is_video_file) { cap.set(cv::CAP_PROP_POS_FRAMES, 0); continue; }
            // 连续坏帧太多 → 管道已经不可信，重开设备（UVC 单进程独占，
            // 这里先 release 再 open，不会和别的进程打架）
            if (g_cam_consec_fail >= CAM_REOPEN_AFTER) {
                g_cam_reopens++;
                std::cerr << "[!] 连续 " << g_cam_consec_fail << " 帧读不出来，第 "
                          << g_cam_reopens << " 次重开 " << dev_path << "\n";
                g_cam_consec_fail = 0;
                // 重开失败就退避 1 秒再试，避免把整个 USB 总线刷爆
                if (!open_camera(cap, dev_path, false, false)) usleep(1000000);
                continue;
            }
            usleep(5000);
            continue;
        }
        auto tB = std::chrono::steady_clock::now();
        frame_id++;

        float scale = 1.f;
        int pad_x = 0, pad_y = 0;
        letterbox(bgr, lb, 640, scale, pad_x, pad_y);
        cv::cvtColor(lb, rgb, cv::COLOR_BGR2RGB);
        auto tC = std::chrono::steady_clock::now();

        rknn_input in{};
        in.index = 0;
        in.buf = rgb.data;
        in.size = 640 * 640 * 3;
        in.type = RKNN_TENSOR_UINT8;
        in.fmt = RKNN_TENSOR_NHWC;

        auto t0 = std::chrono::steady_clock::now();
        rknn_inputs_set(ctx, 1, &in);
        rknn_run(ctx, nullptr);
        for (auto& o : outputs) { memset(&o, 0, sizeof(o)); o.want_float = 1; }
        rknn_outputs_get(ctx, io.n_output, outputs.data(), nullptr);
        auto t1 = std::chrono::steady_clock::now();

        dflfree::decode((const float*)outputs[bi].buf, (const float*)outputs[ci].buf,
                        bnhwc, cnhwc, 640, cfg.min_score, 0.45f, dets);
        auto t2 = std::chrono::steady_clock::now();
        rknn_outputs_release(ctx, io.n_output, outputs.data());

        const double npu_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();
        const double dec_ms = std::chrono::duration<double, std::milli>(t2 - t1).count();

        bool is_1080p = (bgr.cols >= 1920);
        float hfov = is_1080p ? 72.0f : 65.0f;
        float mount_h = is_1080p ? 1.50f : 1.60f;
        float pitch = is_1080p ? 3.5f : 8.0f;
        float range_fwd = is_1080p ? 60.0f : 50.0f;
        float range_lat = 16.0f;
        int radar_size = is_1080p ? 340 : 320;

        BEVProjector bev(bgr.cols, bgr.rows, hfov, mount_h, pitch);

        std::vector<std::pair<std::string, Box3D>> bev_objects;
        int obj_count = 0;

        // ---- 找追踪目标：person 里挑"最可信"的那个 ----
        //
        // 为什么不能只按面积最大（2026-11-03 深夜踩的坑）：
        //   人站得近（<1m）且 tilt 固定 0° 时，头会出画面顶部，
        //   模型只框到脖子/躯干下半 → 框的 cx 反映的是"脖子在哪"，
        //   不是"人在哪"。控制律追着这个错误中心跑，一路撞到 ±90° 限位。
        //
        //   更糟的是：云台转动会让这个不完整的框在画面里移动，
        //   但它移动的量与身体真实方位不成比例 → 形成持续偏差。
        //
        // 判据（三条，按重要性）：
        //   ① 框不能贴住画面边缘（贴边=被裁切=中心不可信）
        //   ② 框的长宽比要像人（站姿约 0.25~0.6）
        //   ③ 在都合格的里挑面积最大
        // 如果一个合格目标都没有，宁愿"不追"，也不要追一个坏目标。
        float best_area = 0.f;
        float tgt_cx = 0.5f, tgt_cy = 0.5f;
        float tgt_frac_w = 0.f;        // 框宽 / 画面宽，用于控制律自适应
        int tgt_clip = 0;              // 贴边方向：0=不贴边, -1=左贴, +1=右贴
        bool has_target = false;
        int rejected_partial = 0;

        const float EDGE_MARGIN = 0.02f;   // 框边距画面边缘 <2% 就算贴边
        const float ASPECT_MIN = 0.15f;    // 宽/高 下限（站姿人约 0.25~0.6）
        const float ASPECT_MAX = 1.20f;    // 上限：太宽可能是坐姿/蹲着/误检

        // 9000 用的逆变换结果（640 → 原图坐标）。单独收集而不是复用循环里的
        // ox1..oy2，因为那些变量在每次迭代里会被复用，循环结束就丢了。
        std::vector<Detection> dets_in_orig;
        dets_in_orig.reserve(dets.size());

        for (const auto& d : dets) {
            float ox1 = (d.x1 - pad_x) / scale;
            float oy1 = (d.y1 - pad_y) / scale;
            float ox2 = (d.x2 - pad_x) / scale;
            float oy2 = (d.y2 - pad_y) / scale;
            ox1 = std::max(0.0f, std::min(ox1, (float)bgr.cols - 1));
            ox2 = std::max(0.0f, std::min(ox2, (float)bgr.cols - 1));
            oy1 = std::max(0.0f, std::min(oy1, (float)bgr.rows - 1));
            oy2 = std::max(0.0f, std::min(oy2, (float)bgr.rows - 1));

            {
                Detection od = d;
                od.x1 = ox1; od.y1 = oy1; od.x2 = ox2; od.y2 = oy2;
                dets_in_orig.push_back(od);
            }

            if (d.class_id == 0) {         // person
                const float bw = ox2 - ox1;
                const float bh = oy2 - oy1;
                const float area = bw * bh;

                // ① 贴边检查：被画面边缘裁掉的框，中心点不可信
                //    分成"哪一侧贴边"，因为两侧的处理方式不同：
                //    右侧贴边 = 目标超出右视野 → 需要往右转（pan 增大）
                //    左侧贴边 = 目标超出左视野 → 需要往左转（pan 减小）
                //    这和"按 cx 追中心"是相反的逻辑：贴边时不能追框的中心，
                //    要追"框被切掉的那一侧"。
                const bool clip_left  = ox1 <= bgr.cols * EDGE_MARGIN;
                const bool clip_right = ox2 >= bgr.cols * (1.f - EDGE_MARGIN);
                const bool clip_top   = oy1 <= bgr.rows * EDGE_MARGIN;
                const bool clip_bot   = oy2 >= bgr.rows * (1.f - EDGE_MARGIN);
                const bool touches_edge = clip_left || clip_right || clip_top || clip_bot;

                // ② 长宽比检查：正常站着的人框是竖长的
                const float aspect = (bh > 1e-3f) ? (bw / bh) : 999.f;
                const bool bad_aspect = (aspect < ASPECT_MIN || aspect > ASPECT_MAX);

                if (cfg.filter_partial && (touches_edge || bad_aspect)) {
                    rejected_partial++;
                    // 仍然画个橙色虚线框，让人知道"检测到了但不追"
                    cv::rectangle(bgr, cv::Point((int)ox1, (int)oy1),
                                  cv::Point((int)ox2, (int)oy2),
                                  cv::Scalar(0, 140, 255), 1);
                    cv::putText(bgr, touches_edge ? "PARTIAL" : "ASPECT",
                                cv::Point((int)ox1 + 4, std::max(14, (int)oy1 - 5)),
                                cv::FONT_HERSHEY_SIMPLEX, 0.45,
                                cv::Scalar(0, 140, 255), 1, cv::LINE_AA);
                } else if (area > best_area) {
                    best_area = area;
                    tgt_cx = (ox1 + ox2) * 0.5f / (float)bgr.cols;
                    tgt_cy = (oy1 + oy2) * 0.5f / (float)bgr.rows;
                    tgt_frac_w = bw / (float)bgr.cols;
                    // 记录贴边方向：放行贴边的目标时（--no-target-filter），
                    // 这个信息用来纠正"追框中心"的错误方向
                    if (clip_right) tgt_clip = +1;
                    else if (clip_left) tgt_clip = -1;
                    else tgt_clip = 0;
                    has_target = true;
                }
            }

            bool is_traffic = is_traffic_class(d.class_id);
            if (!is_traffic && d.score < 0.40f) continue;

            const char* name = (d.class_id >= 0 && d.class_id < 80) ? COCO80[d.class_id] : "obj";
            cv::Scalar col = PALETTE[obj_count % 6];
            obj_count++;

            // ── 2D 检测框始终要画 ──────────────────────────────────
            // 之前只在 3D 反投影失败时才画 2D 框，导致 3D 估算成功时
            // 画面上只剩那个失真的 3D 投影框——看起来"框很小"，让人误以为
            // 检测只框到了脖子。实际检测框是 1014x698（几乎全画面）。
            //
            // 教训：可视化层不能只画"推算结果"，必须同时给出"原始测量"。
            //       否则调试时看到的是估计值，会追着错误的现象排查。
            cv::rectangle(bgr, cv::Point((int)ox1, (int)oy1),
                          cv::Point((int)ox2, (int)oy2), col, 2);

            Box3D box3d;
            bool ground = bev.estimate_3d_box(ox1, oy1, ox2, oy2, name, box3d);
            if (ground && cfg.show_3d) {
                draw_3d_box(bgr, box3d, col, 2);
                bev_objects.push_back({name, box3d});
            }

            // 标签：类别 + 置信度 + 框尺寸（调试时最重要的三个数）
            {
                char label[112];
                snprintf(label, sizeof(label), "%s %.0f%% %.0fx%.0f",
                         name, d.score * 100, ox2 - ox1, oy2 - oy1);
                const int ty = oy1 > 24 ? (int)oy1 : (int)oy1 + 26;
                int baseline = 0;
                cv::Size tsz = cv::getTextSize(label, cv::FONT_HERSHEY_SIMPLEX, 0.55, 1, &baseline);
                cv::rectangle(bgr, cv::Point((int)ox1, ty - 22),
                              cv::Point((int)ox1 + tsz.width + 8, ty), col, -1);
                cv::putText(bgr, label, cv::Point((int)ox1 + 4, ty - 6),
                            cv::FONT_HERSHEY_SIMPLEX, 0.55, cv::Scalar(0, 0, 0), 2, cv::LINE_AA);
            }
        }

        // ---- 发布检测结果到 9000（原图坐标系，与 gateway 一致）----
        // 注意 push 的 dets 是 letterbox 逆变换前的原始输出；publish 内部会
        // 自己做逆变换与边界裁剪，所以这里传 dets 原样即可。
        // 但 dets 用的是 640 坐标，需要先还原 —— 见下面
        publish_detections(frame_id, dets_in_orig, bgr.cols, bgr.rows);

        // ---- 云台控制（每帧都算，但只在变化足够大时才真正下发）----
        if (gimbal_on) {
            if (has_target) {
                lost_frames = 0;

                // ── 目标位置低通滤波（抑制单帧检测抖动）──────────────────
                // 为什么必须有这一步：box 在"头/身体"之间跳时 cx 会抖几十像素，
                // 不做平滑的话控制量跟着抖，云台看起来像抽风。
                // 判据顺序很重要：先用【原始测量】和滤波值比较判跳变，
                // 再更新滤波值。反过来的话永远判不出跳变（滤波值天生贴近上一帧）。
                bool accept = true;
                if (cfg.smooth_alpha > 0.f) {
                    if (!sm_init) {
                        sm_cx = tgt_cx; sm_cy = tgt_cy; sm_init = true;
                    } else {
                        if (fabsf(tgt_cx - sm_cx) > cfg.jump_max) {
                            // 单帧跳变过大 → 判为误检，本帧不用于控制
                            accept = false;
                            jump_skipped++;
                        } else {
                            sm_cx = cfg.smooth_alpha * tgt_cx + (1.f - cfg.smooth_alpha) * sm_cx;
                            sm_cy = cfg.smooth_alpha * tgt_cy + (1.f - cfg.smooth_alpha) * sm_cy;
                        }
                    }
                    tgt_cx = sm_cx;
                    tgt_cy = sm_cy;
                }

                // ── 收敛后暂停（settle）────────────────────────────────
                // 默认 settle_frames=0 → 本段逻辑完全不起作用，行为和"原来"一致：
                // 每帧都算、每帧都可能下发。
                //
                // 只有显式传 --settle N 才启用（用来压过冲）。取舍很清楚：
                //   N>0 稳但慢；N=0 灵敏但可能冲过最优点撞限位。
                bool in_dz = false;
                float np_ = cur_pan, nt_ = cur_tilt;
                const bool may_control = accept && frame_id >= settle_until_frame;
                if (may_control) {
                    in_dz = compute_control(cfg, tgt_cx, tgt_cy, cur_pan, cur_tilt,
                                            np_, nt_, tgt_frac_w);
                }

                locked = in_dz || !may_control;
                if (accept && !in_dz && frame_id >= settle_until_frame) {
                    cur_pan = np_;
                    cur_tilt = nt_;
                    gimbal.set_target(cur_pan, cur_tilt);
                    // 下发后暂停 N 帧，等机械到位 + 画面更新
                    settle_until_frame = frame_id + (uint32_t)cfg.settle_frames;

                    // 在画面上标出追踪目标
                    int bx = (int)((tgt_cx) * bgr.cols);
                    int by = (int)((tgt_cy) * bgr.rows);
                    cv::circle(bgr, cv::Point(bx, by), 10, cv::Scalar(0, 255, 0), 2, cv::LINE_AA);
                    cv::line(bgr, cv::Point(bx - 16, by), cv::Point(bx + 16, by),
                             cv::Scalar(0, 255, 0), 1, cv::LINE_AA);
                    cv::line(bgr, cv::Point(bx, by - 16), cv::Point(bx, by + 16),
                             cv::Scalar(0, 255, 0), 1, cv::LINE_AA);
                } else if (locked) {
                    cv::putText(bgr, "LOCKED", cv::Point(20, bgr.rows - 24),
                                cv::FONT_HERSHEY_SIMPLEX, 1.0, cv::Scalar(0, 255, 0), 2, cv::LINE_AA);
                }
            } else {
                // 目标丢失：短暂保持，超过 ~1.5s（30帧@20fps）就不再动
                // 不要"乱找"——舵机会像抽风一样扫，电机寿命和演示效果都很差
                if (++lost_frames > 30) { locked = false; sm_init = false; }
            }
        }

        // 画面中心十字准星：判断"目标是不是真的在中间"用
        cv::line(bgr, cv::Point(bgr.cols / 2, 0), cv::Point(bgr.cols / 2, bgr.rows),
                 cv::Scalar(60, 160, 60), 1, cv::LINE_AA);
        cv::line(bgr, cv::Point(0, bgr.rows / 2), cv::Point(bgr.cols, bgr.rows / 2),
                 cv::Scalar(60, 160, 60), 1, cv::LINE_AA);

        cv::Mat radar = render_bev_radar(radar_size, range_fwd, range_lat, bev_objects);
        int margin = 16;
        int rx = bgr.cols - radar.cols - margin;
        int ry = margin + 36;
        if (rx >= 0 && ry + radar.rows <= bgr.rows) {
            radar.copyTo(bgr(cv::Rect(rx, ry, radar.cols, radar.rows)));
            cv::rectangle(bgr, cv::Rect(rx - 1, ry - 1, radar.cols + 2, radar.rows + 2),
                          cv::Scalar(0, 200, 255), 1);
        }

        double elapsed = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t_start).count();
        double fps = (double)frame_id / (elapsed + 1e-5);

        char hud[384];
        // 状态词要能区分「真在追」和「卡在限位」。
        // 之前两者都显示 TRACK，导致"撞到底不动"看起来像"正在工作"——
        // 2026-11-03 深夜就因为这个误判，白等了几分钟。
        const char* state_str;
        if (!has_target && rejected_partial > 0) state_str = "NO_GOOD_TGT";
        else if (!has_target)                     state_str = "LOST";
        // ★ 限位判断必须排在 LOCK 之前。
        //   之前把 locked 放前面，导致"撞死在 -90° 还报 LOCK"的假象——
        //   这是最容易骗人的状态：看起来一切正常，其实已经转不动了。
        else if (fabsf(cur_pan) >= cfg.pan_range - 0.5f) state_str = "SATURATED";
        else if (locked)                          state_str = "LOCK";
        else                                      state_str = "TRACK";

        if (gimbal_on) {
            snprintf(hud, sizeof(hud),
                     "EdgeVision RK3588 | NPU %.1f ms + Dec %.1f ms | %.1f FPS | "
                     "Gimbal P%+.1f T%+.1f | %s | objs %zu",
                     npu_ms, dec_ms, fps, cur_pan, cur_tilt,
                     state_str, bev_objects.size());
        } else {
            snprintf(hud, sizeof(hud),
                     "EdgeVision RK3588 | NPU %.1f ms + Dec %.1f ms | %.1f FPS | "
                     "(no gimbal) | objs %zu",
                     npu_ms, dec_ms, fps, bev_objects.size());
        }
        cv::rectangle(bgr, cv::Point(0, 0), cv::Point(bgr.cols, 36), cv::Scalar(15, 23, 42), -1);
        cv::putText(bgr, hud, cv::Point(16, 26), cv::FONT_HERSHEY_SIMPLEX, 0.62,
                    cv::Scalar(56, 189, 248), 2, cv::LINE_AA);

        // 更新 /status 共享状态
        {
            float gp, gt; int ge, gs;
            if (gimbal_on) gimbal.state(gp, gt, ge, gs);
            else { gp = cur_pan; gt = cur_tilt; ge = 0; gs = 0; }
            pthread_mutex_lock(&g_status.lock);
            g_status.pan = gimbal_on ? gp : cur_pan;
            g_status.tilt = gimbal_on ? gt : cur_tilt;
            g_status.locked = locked;
            g_status.sent = gs;
            g_status.errors = ge;
            g_status.objects = (int)bev_objects.size();
            g_status.npu_ms = npu_ms;
            g_status.fps = fps;
            g_status.dropped = g_cam_dropped;
            g_status.reopens = g_cam_reopens;
            pthread_mutex_unlock(&g_status.lock);
        }

        auto tD = std::chrono::steady_clock::now();
        std::vector<uint8_t> jpg;
        // imencode 也是会抛 cv::Exception 的（画布尺寸/通道异常时）。
        // 和断帧一样：不能让它穿出主循环，否则又是一次 terminate。
        // 失败时保留 g_latest_jpeg 里的上一张好图，/stream 不会断，但计数要涨。
        bool jpg_ok = false;
        try {
            jpg_ok = cv::imencode(".jpg", bgr, jpg, {cv::IMWRITE_JPEG_QUALITY, 80});
        } catch (const cv::Exception& e) {
            std::cerr << "[!] JPEG 编码异常（已计数，沿用上一张）: " << e.what() << "\n";
        }
        if (!jpg_ok || jpg.empty()) g_cam_badjpeg++;
        auto tE = std::chrono::steady_clock::now();

        if (frame_id % 60 == 0) {
            auto ms = [](std::chrono::steady_clock::time_point a,
                         std::chrono::steady_clock::time_point b) {
                return std::chrono::duration<double, std::milli>(b - a).count();
            };
            printf("[profile] 采集 %5.1f | 预处理 %4.1f | NPU %5.1f | 解码 %4.1f "
                   "| 画框 %4.1f | JPEG %5.1f || 合计 %5.1f ms | P%+.1f T%+.1f %s"
                   " | 拒绝框 %d | 断帧 %llu 重开 %llu 编码失败 %llu\n",
                   ms(tA, tB), ms(tB, tC), ms(tC, t1), ms(t1, t2),
                   ms(t2, tD), ms(tD, tE), ms(tA, tE),
                   cur_pan, cur_tilt, state_str, rejected_partial,
                   (unsigned long long)g_cam_dropped, (unsigned long long)g_cam_reopens,
                   (unsigned long long)g_cam_badjpeg);
            fflush(stdout);
        }

        // ★ 只有真的编出图才发布。
        //   原来无条件 `g_latest_jpeg = std::move(jpg)`：编码失败时 jpg 为空，
        //   这一行会把上一张好图也清掉 → /stream 的 `if (jpeg_copy.empty()) continue;`
        //   会把客户端挂住，网页表现为"画面冻结且不再更新"。
        if (jpg_ok && !jpg.empty()) {
            pthread_mutex_lock(&g_jpeg_lock);
            g_latest_jpeg = std::move(jpg);
            pthread_cond_broadcast(&g_jpeg_cond);
            pthread_mutex_unlock(&g_jpeg_lock);
        }
    }

    cap.release();
    rknn_destroy(ctx);
    pthread_cond_broadcast(&g_jpeg_cond);
    pthread_join(http_tid, nullptr);
    return 0;
}
