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
//  ⚠️ invert-pan 是实测结论，不是可选参数 —— 但它和 --rotate 强耦合
//
//      控制律（compute_control）是：
//          d_pan = +dx * fov_pan * kp_pan     （dx = cx - 0.5，目标偏右 dx>0）
//          if (invert_pan) d_pan = -d_pan;
//      ⇒ invert_pan=true  = 「目标偏右 → pan 减小」
//      ⇒ invert_pan=false = 「目标偏右 → pan 增大」
//
//      两次独立标定（2026-11-03 三位置：pan=−15/0/+15 → cx=746.9/878.6/947.2，
//      即 pan↑ → cx↑）都是在**未旋转**的画面上做的，结论是「正反馈，必须翻转」。
//
//      ★ 但 --rotate 180（摄像头倒装）会翻转一次符号：
//          cv::flip(bgr, rot, -1)  ⇒  cx_rotated = W - cx_raw
//          原始帧「偏右」在旋转后变成「偏左」，x 方向整体镜像。
//          所以：raw 坐标系 invert_pan=true，180° 坐标系 invert_pan=false。
//      本程序实际就是带 --rotate 180 跑的，故运行时值为 false。
//
//      该值现在**不再写死**：main 里按 g_rotate 自动推导
//      （0→true，180→false，90/270 未标定则告警并用 true），
//      命令行 --invert-pan / --no-invert-pan 可以显式覆盖。
//      硬件改动后要重测（见 README 8.6.1）。
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
//
//  分轴参数（2026-11-16 tilt 振荡修复，★ 别再合并成一套）：
//      --max-step N         pan 轴单帧步长上限，默认 0.6 单位 = 0.6×42.7 ≈ 25.6 px
//      --max-step-tilt N    tilt 轴单帧步长上限，默认 0.20 单位 = 0.20×80.0 = 16.0 px
//      --deadzone N         pan 死区（整幅宽归一化），默认 0.025 → 0.025×1280 = 32 px
//      --deadzone-tilt N    tilt 死区（整幅高归一化），默认 0.035 → 0.035×720 = 25.2 px
//      --start-pan N        ★ 启动姿态 pan（2026-11-17 新增）
//      --start-tilt N       ★ 启动姿态 tilt
//                           为什么需要：舵机固定后中位可能看不到人，
//                           不先摆过去就会往错误方向追、顶到限位卡死。
//  为什么必须分开：固件 PAN_RANGE_DEG=90 / TILT_RANGE_DEG=45，都对应物理 180° 全行程，
//  所以 pan 1 指令单位 = 2° 物理角、tilt 1 指令单位 = 4° 物理角（是 pan 的 2 倍）。
//  折算画面像素：pan 1280/60×2 = 42.7 px/单位，tilt 720/36×4 = 80.0 px/单位（1.87 倍）。
//  共用 max_step=0.6 时 tilt 单帧走 48px > 死区 32px ⇒ 每帧冲出死区 ⇒ 欠阻尼极限环，
//  现象就是用户说的"蹲下站起来，云台上仰要上下摆一会才停"。详见 compute_control。
// ============================================================================
#include <iostream>
#include <vector>
#include <queue>
#include <chrono>
#include <cstring>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cmath>            /* sqrtf：变步长（见 compute_control 里的 slow_gain 缩放） */
#include <unistd.h>
#include <fcntl.h>
#include <termios.h>
#include <pthread.h>
#include <csignal>
#include <sys/select.h>
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

        // ── P1（2026-11-16）：设一次帧间插值 ────────────────────────
        //
        // 舵机的默认行为是"收到新角度就立刻跳过去"，于是 30FPS 下：
        //     跳一下 → 静止 33ms → 再跳一下 ……  肉眼看是"一顿一顿"
        //
        // 发 S <ms> 让固件把角度变化摊成指数缓动。取 40ms ≈ 一帧时间，
        // 正好让舵机在这一帧到下一帧之间平滑过渡。
        //
        // 实测依据：舵机机械响应 230~364ms（10°~30° 步进），远大于 40ms。
        // 所以 40ms 缓动不会"拖慢"响应——舵机本身就跟不上更快的变化，
        // 只是把原来"跳一下"的突变改成了小步连续移动，观感更顺。
        {
            const char* slew = "S 40\n";
            if (write(fd_, slew, strlen(slew)) > 0) {
                printf("[gimbal] 已设置帧间插值 S 40ms（一帧时间，消除跳动感）\n");
                // 等 S 命令的回包并清掉（只在上电时做一次）。
                //
                // ★ 这里绝对不能写 while (read(fd_, ...) > 0) —— fd_ 在桥接模式下
                //   是 **TCP socket**，没数据时 read() 永久阻塞，而固件只回两行，
                //   于是死循环把主线程挂死（2026-11-16 实测：启动横幅打到这一行就没了）。
                //   必须用 select 加超时。
                usleep(120000);       // 先等固件处理完（S 命令要 20ms 节拍）
                {
                    struct timeval stv;
                    stv.tv_sec = 0;
                    stv.tv_usec = 300000;      // 最多等 300ms
                    char tb[256];
                    // 一次 select 就够：把缓冲区里现有的全读出来
                    fd_set sfds;
                    FD_ZERO(&sfds);
                    FD_SET(fd_, &sfds);
                    if (select(fd_ + 1, &sfds, nullptr, nullptr, &stv) > 0) {
                        int g = read(fd_, tb, sizeof(tb) - 1);
                        (void)g;
                    }
                }
            }
        }
        running_ = true;
        pthread_create(&thread_, nullptr, &GimbalSerial::thread_entry, this);
        return true;
    }

    // 给启动姿态用的裸 fd（2026-11-17）
    //   为什么需要：启动时要直接发一条 P 命令摆姿态，但 set_target 是异步的
    //   （只写目标、由串口线程发），没法确认"发出去"和"到位"。
    //   这里暴露 fd 让 main 直接写一条并等待。
    int fd_for_start() const { return fd_; }

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

            // ── P3（2026-11-16）：回包读取改成**非阻塞** ──────────────
            //
            // 原来这里是阻塞 read()，等桥接把固件的回包送回来。实测：
            //     串口命令往返 RTT  p50 = 19.2ms / p90 = 19.5ms / max = 39.1ms
            // 而流水线跑起来后单帧才 30ms 左右 —— **一半的帧时间耗在等回包上**。
            //
            // 为什么可以不等：
            //   回包只有一个用途——确认"固件收到了这个角度"。
            //   但我们每帧都发新目标（滑动平均后），就算某条丢了，
            //   下一帧立刻补上，观感上完全无感。
            //   反过来，阻塞等待会让控制线程掉到 20~30Hz 且有 20ms 抖动。
            //
            // 做法：
            //   1. 发完立刻尝试读一次（O_NONBLOCK 语义，最多等 2ms）
            //   2. 读到了就更新 pan_/tilt_，读不到就认为"在途"，不阻塞
            //   3. 在途计数超阈值才记 error（区分"偶发丢包"和"链路真坏了"）
            //
            // 注意：fd_ 本身不能改成 O_NONBLOCK（会破坏 termios 的行读语义），
            //       所以用 select() 加 2ms 超时来模拟非阻塞读。
            // ★ 必须把回包读**干净**，不能只读一次。
            //   2026-11-16 实测踩到：只读一次的话，一次 P 命令的回包如果分两次
            //   到达（"OK ..." 之外还有 "# slew" 前缀行等），剩余部分会留在
            //   socket 缓冲区里。外部监控脚本（如发 I / Q 的调试命令）连上来
            //   后先读到这些陈旧回包，造成**整体错位一条**：
            //       I -> ["OK 30.4 -20.0"]   ← 本该是 GPIN 14 15
            //       Q -> ["GPIN 14 15"]      ← 本该是 S ...
            //
            //   做法：select 轮询，直到没有新数据为止。总时间上限 2ms。
            //   这样既非阻塞，又不留残留。
            {
                struct timeval tv;
                fd_set rfds;
                char buf[256];
                int total_got = 0;
                // 最多轮询 4 次，每次最多 0.5ms —— 避免异常情况下死循环
                for (int round = 0; round < 4; ++round) {
                    FD_ZERO(&rfds);
                    FD_SET(fd_, &rfds);
                    tv.tv_sec  = 0;
                    tv.tv_usec = 500;
                    if (select(fd_ + 1, &rfds, nullptr, nullptr, &tv) <= 0) break;

                    int got = read(fd_, buf, sizeof(buf) - 1);
                    if (got <= 0) break;
                    buf[got] = '\0';
                    total_got += got;

                    // 解析（只认第一行，够用了）
                    if (strncmp(buf, "OK", 2) == 0) {
                        float p, t;
                        if (sscanf(buf + 2, "%f %f", &p, &t) == 2) { pan_ = p; tilt_ = t; }
                        else { pan_ = tp; tilt_ = tt; }
                    } else if (strstr(buf, "out_of_range")) {
                        pan_ = tp; tilt_ = tt;
                    }
                    // 其它内容（"# slew" 注释行等）静默忽略，但**已经读出来丢掉了**
                }

                if (total_got == 0) {
                    // 没收到回包：不阻塞，乐观认为固件会执行（下帧会纠正）
                    pan_ = tp; tilt_ = tt;
                    // 在途累积过多才认为链路真的有问题
                    if (++pending_ > 10) { errors_++; pending_ = 0; }
                } else {
                    pending_ = 0;
                }
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
    int pending_ = 0;      // P3: 连续多少次没收到回包（超 10 才记 error）
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
    // ── 历史：默认值曾是 run_tracking.ps1 实测标定的那套（2026-11-03）──────
    // 那套值在真机上跑过：60 秒 223 条指令、零错误、77 次锁定。
    //
    // 中途为了压住"冲限位"加过三样东西（settle 暂停 / 低通滤波 / 质量筛选），
    // 结果响应慢到用户能明显感觉到"不灵敏"。现已全部**默认关闭**。
    //
    // 教训：防过冲的手段如果代价是"几乎不响应"，那还不如不过冲。
    //       正确的顺序是先把目标框弄对（人站远/接 tilt），再谈抑制震荡。
    //       —— 这正是 2026-11-16 这次改动做的事：先修目标框（追头部 +
    //          放行底边贴边），再按实测重新定 kp/deadzone。
    // ── 2026-11-16 重新定档：按"控制律是积分型 + 舵机机械极限"推导 ──────
    //
    // ★ 先说结论的由来：本次默认值**不是**拍脑袋调的，也不是照搬 Python 版。
    //   原任务书写的是 kp_pan=0.35 / max_step=4.0，但实测数据推翻了那个前提，
    //   这里是按本机实测重新推的。两者冲突点逐条写清楚，方便后人复核。
    //
    // ① 控制律是**积分型（增量式）**，不是位置式 —— 这一条决定 kp 怎么取：
    //        out_pan = pan + kp * (cx-0.5) * FOV      // 每帧累加在当前位置上
    //    即 d(pan)/dt = kp·err/T_frame，开环 L(s) = K·e^(-sTd)/(s(1+τs))，**K = kp/T_frame**。
    //    ⇒ 同一个 kp，在不同帧率下**行为完全不同**。
    //    Python 版在 ~2 FPS 上调到 kp=0.5（K≈1.0）；本程序实测 25.6 FPS，
    //    若照搬 0.5 则 K≈12.8，是前者的 12 倍 —— 必然振荡。
    //    所以"kp 提到 0.35"这个建议的前提（以为 23FPS 和 2FPS 可比）不成立。
    //
    // ② kp 的取值依据临界增益：实测 kp_u ≈ 0.37~0.75，取 45~60% ⇒ **0.20**。
    //    kp_tilt 取 **0.13**：垂直 FOV 36° < 水平 60°，且 tilt 有重力负载、
    //    安全范围仅 ±35°，过冲后果比 pan 更危险，故比按比例外推再保守一点。
    //
    // ③ max_step 有**两条**约束，之前只看了机械速度那一条，漏了更要命的那条：
    //
    //    ③a 机械速度上限（旧结论，仍然成立）：
    //       实测舵机每帧（39.1ms）只能转 1.8~3.3°（≈150°/s，含 12ms 启动 + 140ms 固定开销）。
    //       max_step=2.5 对应 64°/s，落在舵机能力 46~85°/s 中间；4.0 会跑在机械前面。
    //
    //    ③b 死区稳定条件（旧版漏掉，2026-11-16 补上 —— 这才是"人不动云台却在摆"的根因）：
    //       量级：画面 1280px、水平 FOV 60°；
    //         pan 1 单位 = 2° 物理角（PAN_RANGE_DEG=90 对应物理 180°）
    //                    → 2°/60° × 1280 = **42.7 px** 画面位移
    //       ★ 死区的像素换算必须先看清归一化约定，这里以前算错过一次：
    //         主循环 tgt_cx = (ox1+ox2)*0.5f / bgr.cols  ⇒ cx ∈ [0,1] 以**整幅宽**归一化，
    //         所以 dx = cx-0.5 里 1.0 个单位 = 1280px，**不是 640px**。
    //         旁证：本函数下方旧注释「固定死区只有 ±64px」对应 deadzone 0.05，
    //               0.05 × 1280 = 64 ✓ 同一约定。
    //         （Python 版 ex=(hx-W/2)/(W/2) 是 [-1,1] 半宽归一化，那里才乘 640；
    //           两套约定等价换算 = Python 的 deadzone 0.05 ⇔ 这里的 0.025。）
    //       ⇒ deadzone = 0.025 × 1280 = **32 px**
    //       ⇒ max_step=2.5 单位 → 单帧最大 2.5 × 42.7 = **107 px**，是死区的 **3.3 倍**
    //
    //       稳定条件要求「单帧最大位移 ≤ 死区 × 0.5」，否则每帧都冲出死区 → 反向追
    //       → 再冲出 → **极限环**（人站着一动不动，云台持续来回摆 = 用户说的"抽风"）。
    //       按这个条件反解 max_step 上限 = (32 × 0.5) / 42.7 ≈ **0.375 单位**。
    //
    //       闭环仿真（积分器 + 纯延迟，目标偏 300px）复核了这个条件：
    //         延迟 3 帧(117ms) 旧参数已超调 239px；延迟 4 帧(156ms) 起进入持续振荡。
    //         实测链路延迟 150~300ms ⇒ 旧参数**必然**振荡，与现场现象一致。
    //
    //    ③c 那为什么不直接把 max_step 定成 0.375？
    //       因为那会变成"追得很慢"（用户反馈过"反应不够快"）：
    //       0.375 单位 = 16px/帧，追 300px 误差要 ~19 帧 ≈ 0.75 秒，且延迟下仍会过冲。
    //       两个要求（近处不能振、远处要快）在**固定**步长下无法同时满足。
    //       ⇒ 解法是**变步长**（下面 slow_gain + compute_control 里那段 sqrt 缩放）：
    //         近处自动收小到 0.35 倍（0.6×0.35 = 0.21 单位 ≈ 9px < 16px 死区一半 ✓ 不振），
    //         远处用满上限 0.6 单位（25.6px，比 0.375 快 1.6 倍 ✓ 够快）。
    //       所以 max_step 取 0.6（1.6 倍于理论稳定上限）是**故意**的：
    //       稳定条件由「近处那段被 sqrt 缩小的步长」保证，而不是由 max_step 保证。
    //
    // ④ deadzone 从 0.05 降到 **0.025** —— 这是"跟不紧"的真正元凶，不是 kp：
    //    下方 compute_control 里有 dz_scale 自适应放大，人离 1m 时框占画面 60~79%
    //    → dz_scale ≈ 1.29~1.50 → 实际死区 = 0.05×1.5 = 0.075 → 角度死区 3.9~4.5°
    //    → 1m 处横向盲区高达 ±70~79cm，**人走几步云台都不动**。
    //    降到 0.025 后盲区缩到 ±28~47cm。
    //    下限受测量噪声约束：近距离框中心抖动 ±20px ≈ 0.94°，所以 deadzone 不可低于 0.017。
    //    注：**没有**去改 dz_scale 那段逻辑（它"目标大=靠近=少动作"的思路本身合理），
    //    只降基准值，风险最小。
    //
    // ⑤ 不加阻尼/低通：实测 Td ≈ 88ms 仅 2.2 帧，阻尼收益小、代价是明显变慢
    //    （用户已反馈"加滤波就变慢"）。--smooth 保持默认关。
    float kp_pan = 0.20f;
    float kp_tilt = 0.13f;
    float fov_pan = 60.0f;
    float fov_tilt = 36.0f;
    float deadzone = 0.025f;        // pan 用：0.025 × 1280 = **32 px**（推导见上面 ③b）
    // ★★ tilt 独立死区（2026-11-16 tilt 振荡修复）★
    //
    // 为什么 tilt 的死区要和 pan 分开，而不是"归一化后本来就等效、不用管"：
    //   表面看 dx/dy 都按整幅归一化（cx/width、cy/height），1 个归一化单位
    //   = 1280px（横）/ 720px（竖），好像"0.025 就都是 2.5% 画面"。
    //   **但这不等于控制上是等效的** —— 因为归一化只统一了"像素 → 比例"，
    //   没有统一"比例 → 舵机指令"这一步，而两个轴的换算系数差 2 倍：
    //     pan : 1 指令单位 = 2° 物理角 → 2°/60°(水平FOV) × 1280px = 42.7 px
    //     tilt: 1 指令单位 = 4° 物理角 → 4°/36°(垂直FOV) ×  720px = 80.0 px
    //   于是同一个 deadzone=0.025 换算成"舵机指令单位"分别是：
    //     pan : 32px / 42.7px = **0.75 单位**
    //     tilt: 18px / 80.0px = **0.225 单位**   ← 只有 pan 的 1/3，**tilt 更紧**
    //   ⇒ tilt 比 pan 更容易被判"出死区"，每帧更容易触发修正 ⇒ 加重振荡。
    //   所以 tilt 还吃一个更大的死区来补偿，把它的指令单位死区拉回同量级：
    //     0.035 × 720 = 25.2px  = 25.2/80.0 = **0.315 单位**（pan 0.75 单位的 42%）
    //   再大就回到"跟不紧"的老问题（④ 那一段：近距离盲区已经很难受了），
    //   0.035 是在"补偿灵敏度"和"别又变迟钝"之间的折中。
    //   另一个理由：人体蹲起是**低频大幅**动作，本来就不需要追那么细；
    //   把 tilt 死区放大反而让"蹲下站起来"这种输入下云台更愿意等一等再动。
    float deadzone_tilt = 0.035f;   // tilt 用：0.035 × 720 = 25.2 px（见上面推导）
    // 单帧步长上限（单位=舵机指令单位，不是像素）。2026-11-16 从 2.5 降到 0.6，理由见 ③b / ③c。
    //
    // ★★ 这里必须分轴！pan 与 tilt 的"单位 → 像素"换算差 1.87 倍 ★★
    //   （2026-11-16 tilt 振荡根治改动：原来两轴共用 max_step=0.6，tilt 必振）
    //   固件定义（esp32_gimbal/main/gimbal_main.c）：
    //       #define PAN_RANGE_DEG   90.0f   // 指令 ±90 = 物理 ±90°（180° 全行程）⇒ 1 单位 = 2° 物理角
    //       #define TILT_RANGE_DEG  45.0f   // 指令 ±45 = 物理 ±90°（180° 全行程）⇒ 1 单位 = 4° 物理角
    //   ⇒ tilt 1 指令单位转的物理角是 pan 的 **2 倍**，这是两个轴唯一的本质差异。
    //   折算到画面像素（水平 FOV≈60°、宽 1280；垂直 FOV≈36°、高 720）：
    //       pan : 1280/60 × 2 = **42.7 px / 单位**
    //       tilt:  720/36 × 4 = **80.0 px / 单位**   ← 是 pan 的 1.87 倍
    //
    //   共用 0.6 时的单帧最大位移（死区 32px，稳定性要求"单帧位移 ≤ 死区×0.5 = 16px"）：
    //       pan : 0.6 × 42.7 = 25.6 px  < 32px       勉强稳（靠 slow_gain 在近处收小）
    //       tilt: 0.6 × 80.0 = 48.0 px  > 32px 冲出 1.5 倍  ⇒ **每帧都越过死区 → 极限环** ★
    //   按稳定条件反解两个轴各自的 max_step 上限：
    //       pan  上限 = 16px / 42.7 = 0.375 单位
    //       tilt 上限 = 16px / 80.0 = 0.200 单位   ← tilt 沿用 0.6 是超标的 3 倍
    //   ⇒ pan 保持 0.6（③c：故意大于理论上限，靠 near 段 slow_gain 收小，远处才够快）；
    //     tilt 取 **0.20**，正好落在它自己的稳定上限上，不再靠"反正有 0.35 倍的近处收缩"兜。
    //     tilt 为什么保守到直接贴上限、而不是也留 1.6 倍余量：
    //       tilt 有重力负载（上仰要抬、下俯要跟），可用的加速度余量比 pan 小；
    //       且 tilt 的可用角只有 tilt_safe [-35,-5] 共 30°，过冲空间比 pan(±90°)小得多，
    //       振起来更容易撞软件限位。**"上下摆好几轮才停"正是 tilt 欠阻尼的直接表现。**
    //   ⇒ 以后再有人想"两个轴统一参数、代码简单点"，请先看上面这 5 行换算。
    float max_step = 0.6f;          // pan 用（42.7 px/单位）
    float max_step_tilt = 0.20f;    // ★ tilt 单独设：tilt 1 单位 = 80px（pan 的 1.87 倍）
    // 变步长的"临界处步长比例"：误差刚出死区时只走满量程的 35%，误差大时用 sqrt
    // 平滑过渡到 100%。这个数字直接决定稳定条件是否成立 —— 改它之前先回去重算 ③b。
    float slow_gain = 0.35f;
    float pan_range = 90.0f;
    float tilt_range = 45.0f;
    // tilt 硬保护区间（2026-11-16 新增）
    //   为什么要有：之前出现过 tilt 一路跑到限幅、镜头朝天的情形（负反馈变正反馈时
    //   tilt 会单调往外走，直到撞上 tilt_range=±45°）。tilt_range 是**机械**限位，
    //   ±45° 里有一部分角度虽然不撞机械、但镜头已经对着天花板/地板，画面里根本没有人。
    //   实测能稳定看见人的区间约 -30 ~ -8，这里取更宽的 -35 ~ -5 做软件兜底：
    //   只在控制输出处钳位，不影响 tilt_range 的机械保护语义。
    // ── 启动姿态（2026-11-17 新增）──────────────────────────────
    //
    // 为什么需要：
    //   舵机固定后，上电中位是 P0 T0（朝天花板），看不到人。
    //   程序启动时沿用当前姿态，如果看不到人就往错误方向一直追，
    //   最后顶到 P-90 限位卡死（实际发生过，用户反馈"停在那里动不了"）。
    //
    // 用法：
    //   --start-pan 5 --start-tilt -20
    //   启动时先摆到这个姿态再开始追踪。
    //
    // 默认 NAN = 不启用（沿用当前姿态，保持向后兼容）。
    float start_pan  = NAN;
    float start_tilt = NAN;

    float tilt_safe_min = -35.0f;
    float tilt_safe_max = -5.0f;
    // ★ invert_pan 与 --rotate 强耦合，推导见文件头注释，结论：
    //     本程序实际用 --rotate 180 启动（摄像头倒装，flip 把 cx 镜像成 W-cx），
    //     Python 侧在**原始帧**测得「目标偏右 → pan 必须减小」⇒ 原始帧 invert_pan=true；
    //     旋转 180° 后「偏右」正好是原始帧的「偏左」，符号再翻一次 ⇒ **这里必须是 false**。
    //   ⚠️ 不带 --rotate 180 启动时这个值就是错的（会变正反馈、几秒顶死限位），
    //      启动时程序会检查并打印醒目警告（见 main）。
    bool invert_pan = false;
    // ★ invert_tilt = **false**（2026-11-16 由 true 改成 false，已实测确认）
    //
    //   【历史】这里原来是 true，注释写着"tilt 轴第二颗舵机至今没有做过方向标定，
    //   保持历史默认值；装上舵机后必须用 --no-invert-tilt 试一次再定。★ 未经实测确认"。
    //   现在那个"未经实测确认"的债还上了，推导如下（在 Python 版上验证过）。
    //
    //   【实测标定数据】tilt 与 person 框在画面里的 y1（框顶 = 头）位置：
    //       tilt=-14 → 框顶 y1=404   （头在画面下部）
    //       tilt=-20 → 框顶 y1=29    （头到画面顶部）
    //   结论：**tilt 减小 → 头往画面上方移动**（等价于相机低头）。
    //   注：本程序用 --rotate 180 启动，下面的 y1/cx 语义都已在旋转后的坐标系里。
    //
    //   【控制律】d_tilt = -err_tilt * kp_tilt，其中 err_tilt = tgt_cy - 0.5
    //       （负 = 头在画面上方，注意 tgt_cy 追的是"头"而不是框中心，见主循环）
    //
    //   【逐种情况推一遍】
    //     · 头在上方 → err_tilt < 0
    //       要让头往下移 → 按实测就得让 tilt **增大**
    //       d_tilt = -err_tilt * kp，err_tilt<0 ⇒ d_tilt>0 ✓ 方向本来就对
    //     · 再看 `if (invert_tilt) d_tilt = -d_tilt;` 这一行：
    //         invert_tilt=true  → d_tilt 被取负 → tilt 减小 → 头更往上 → **正反馈，顶死限幅**
    //         invert_tilt=false → d_tilt 保持正 → tilt 增大 → 头往下   → **负反馈，正确**
    //
    //   ⇒ **invert_tilt 必须是 false**，与 invert_pan 当前值一致（invert_pan 别动）。
    //   和 invert_pan 一样，这个值也可以被 --invert-tilt / --no-invert-tilt 覆盖，
    //   换硬件/改安装后要重测（方法见 README 8.6.2）。
    bool invert_tilt = false;
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
    // ★ 分轴取参数（2026-11-16 tilt 振荡修复）—— 别再合并回一个变量：
    //   两个轴的"指令单位 → 像素"换算差 1.87 倍（pan 42.7px/单位 vs tilt 80px/单位，
    //   推导见 ControlCfg 里 max_step 上方那段）。共用同一套 deadzone/max_step 时，
    //   tilt 的实际步长是 pan 的 1.87 倍、实际死区反而只有 pan 的 1/3 量级 ⇒ tilt 必振。
    //   这里 pan 继续用 c.deadzone / c.max_step（行为与改动前**完全一致**，便于对照回归），
    //   tilt 用自己的 c.deadzone_tilt / c.max_step_tilt。
    const float deadzone_pan  = c.deadzone      * dz_scale;
    const float deadzone_tilt = c.deadzone_tilt * dz_scale;
    const float max_step_pan  = c.max_step      * step_scale;
    const float max_step_tilt = c.max_step_tilt * step_scale;

    if (fabsf(dx) < deadzone_pan)  dx = 0.f;
    if (fabsf(dy) < deadzone_tilt) dy = 0.f;
    if (dx == 0.f && dy == 0.f) {
        out_pan = pan; out_tilt = tilt; return true;
    }

    float err_pan = dx * c.fov_pan;
    float err_tilt = dy * c.fov_tilt;

    float d_pan = err_pan * c.kp_pan;
    float d_tilt = -err_tilt * c.kp_tilt;
    if (c.invert_pan) d_pan = -d_pan;
    if (c.invert_tilt) d_tilt = -d_tilt;

    // ── 变步长：误差刚出死区时步子小（防振），误差大时用满上限（够快）──
    //
    // 为什么必须有这一段（2026-11-16，用户反馈"人不动云台却来回摆"）：
    //   固定 max_step 无法同时满足两个要求：
    //     · 步子必须远小于死区才不会振荡（见 ControlCfg 里 ③b 的量级推导：
    //       max_step=2.5 → 单帧 107px，是 32px 死区的 3.3 倍；叠加闭环延迟后必然极限环）
    //     · 但步子压到稳定值（≈0.375 单位）后追 300px 误差要 19 帧（0.75 秒），"反应太慢"
    //   解法：让步长跟着"离死区多远"走，近处自动收小、远处放开。
    //
    // 公式（照搬 Python 版 _track_head_v3.py 已验证的写法，但**归一化分母必须改**）：
    //     ratio = (|dx| - deadzone) / (0.5 - deadzone)   0（刚出死区）→ 1（画面边缘）
    //     scale = slow_gain + (1 - slow_gain) * sqrt(ratio)
    //     d    *= scale
    //
    //   ★ 为什么分母是 (0.5 - deadzone) 而不是 (1.0 - deadzone) —— 这条别改错：
    //     Python 版用的是 ex = (hx - W/2)/(W/2)，那是 [-1, 1] 归一化，|ex| 最大 1.0；
    //     本文件的 dx = cx - 0.5，cx = x/width ∈ [0,1]，所以 |dx| 最大 **0.5**。
    //     若照抄 (1.0 - deadzone)，ratio 最大只到 (0.5-0.025)/(1-0.025) ≈ **0.487**，
    //     即 scale 永远到不了 1，满量程步长只有 0.48 而不是 0.6 —— "远处够快"那半条设计失效。
    //     （实测对照：分母 1.0 时画面边缘 scale=0.804、步长 25.6px；
    //       分母 0.5 时 scale=1.000，中段 64px 处 11.6→12.8px，过渡更陡一点。）
    //   · 用 sqrt 而不是线性：在 ratio→0 处斜率无穷大，离开死区时步长"抬得快"，
    //     所以不会出现"刚出死区、每帧只挪 1px、卡在边界爬行"的黏滞感；
    //     同时根号又保证近处整体幅度仍然很小 —— 兼具"平滑"和"不黏"。
    //   · ratio 分母保护：(0.5 - deadzone) 恒 > 0（deadzone 是 0.0x 量级），
    //     但 --deadzone 传大了会击穿，这里加 deadzone<0.5 的显式判断 + 1e-6 兜底。
    //
    // 数值核对（deadzone=0.025 → 32px, slow_gain=0.35, max_step=0.6, 42.67px/单位）：
    //     刚出死区 33px  ratio=0.002 → scale=0.376 → 0.12 单位 ≈  5.0 px  （死区一半=16px ✓）
    //     40px           ratio=0.013 → scale=0.425 → 0.16 单位 ≈  6.8 px  ✓
    //     64px           ratio=0.053 → scale=0.499 → 0.30 单位 ≈ 12.8 px  ✓
    //     80px           ratio=0.079 → scale=0.533 → 0.40 单位 ≈ 17.0 px（误差仍 80px，不跨死区）
    //     100px          ratio=0.112 → scale=0.567 → 0.53 单位 ≈ 22.7 px
    //     200px 以上     ratio>0.27  → scale>0.69 → 命中 max_step 0.6 ≈ 25.6 px
    //   ⇒ 近处（偏差 <70px）步长稳在死区一半以内，远处满量程 25.6px/帧。
    //   稳定的真判据是「走一步后不越过另一侧死区边界」：d(e) < e + deadzone。
    //   仿真逐一核对 e=33..300px 全部满足；旧 max_step=2.5 在 e=33px 时走 13.2px 也满足，
    //   **所以纯无延迟时旧参数并不振** —— 旧参数之所以必然振荡，是因为闭环延迟：
    //     旧 107px/帧 × 延迟 4~8 帧 = 累计 427~853px > 典型误差 300px ⇒ 必冲过头
    //     新 26px/帧  × 延迟 4~8 帧 = 累计 102~205px < 300px               ⇒ 安全
    //   闭环仿真（积分器+纯延迟）：延迟 4~6 帧(156~235ms) 旧参数末段振幅 683~1109px（振），
    //   新参数 0px（稳）；延迟 8 帧(313ms) 两者都开始振 —— 那是链路延迟本身的极限，
    //   单靠参数调不动，见报告里的延迟分析。
    //   （注意 kp 的原始输出在 |dx|>0.05（=64px）时已超过 0.6，所以那以后步长
    //     实际由 max_step×scale 决定，与 kp 无关 —— 调 kp 只影响近处那一段。）
    //
    // ── tilt 轴数值核对（2026-11-16 tilt 振荡修复，分轴后重算）────────────
    //   tilt 参数：deadzone_tilt=0.035 → 0.035×720 = **25.2px** 死区，
    //              slow_gain=0.35, max_step_tilt=0.20, **80.0px/单位**, kp_tilt=0.13。
    //   原始控制量（未乘 scale）：d_raw = 0.13 × dy×36，在 dy>0.0427(=30.8px) 时
    //   就超过 0.20，但那只是"理论上限"；**实际**被 max_step_tilt 钳住要再乘上
    //   scale（近处 0.38、远处 1.0），解得真正的钳位起点 = **56.2px**。
    //   偏差 e → 单帧步长 d（下列数值已由独立测试程序逐点实测确认）：
    //      20px（< 25.2px 死区） → **0.000 px**（不动）
    //      26px（刚出死区）      → ratio=0.0024 scale=0.382 → 0.065 单位 = **5.16 px**
    //      30px                  → ratio=0.0143 scale=0.428 → 0.083 单位 = **6.67 px**
    //      40px                  → ratio=0.0442 scale=0.487 → 0.127 单位 = **10.12 px**
    //      50px                  → ratio=0.0741 scale=0.527 → 0.171 单位 = **13.70 px**
    //      100px                 → ratio=0.2234 → 钳到 0.20 单位 = **16.00 px**
    //      200px                 → ratio=0.5221 → 钳到 0.20 单位 = **16.00 px**
    //      300px → 画面边缘      → ratio→1.0    → 钳到 0.20 单位 = **16.00 px**
    //   ⇒ **tilt 单帧位移上限 = 16.0px**（= 0.20 单位 × 80 px/单位），
    //     正是"死区 32px × 0.5"这条量级的对应值，且**不再放大**：
    //     改之前 0.6×80 = 48px 会越过另一侧死区边界（32px）；
    //     现在 16px 相对 25.2px 死区是 0.63 倍，走一步后仍停在死区外侧 ⇒ 不反向超调。
    //     （真判据 d(e) < e + deadzone 在 e=1..360px 全域成立，见测试程序 ⑤；
    //       注意 e→deadzone 时 d→0，单调无过冲。）
    //     框宽自适应会把上限抬到 0.20×1.3×80 = **20.8px**（远处框小时 step_scale=1.3），
    //     但那时 dz_scale 也同时降到 0.6（死区 17.4px），比值 1.19 —— 见测试程序 ⑧，
    //     这是原有自适应机制的固有特性（pan 同理，本轮未改动）。
    //   pan 一路未动，数值与上面那张表逐条相同 —— 回归时可直接对账。
    if (deadzone_pan < 0.5f) {
        if (fabsf(dx) >= deadzone_pan) {
            float ratio = (fabsf(dx) - deadzone_pan) / (0.5f - deadzone_pan + 1e-6f);
            if (ratio > 1.0f) ratio = 1.0f;
            if (ratio < 0.0f) ratio = 0.0f;     // 浮点兜底，理论上不会进来
            d_pan *= (c.slow_gain + (1.0f - c.slow_gain) * sqrtf(ratio));
        }
    }
    // tilt 的 ratio 用**自己的**死区做分母，否则刚出 tilt 死区那一段会被算成"已到画面边缘"
    // （deadzone_tilt > deadzone_pan ⇒ 分子偏大），近处收不小、白丢半条防振设计。
    if (deadzone_tilt < 0.5f) {
        if (fabsf(dy) >= deadzone_tilt) {
            float ratio = (fabsf(dy) - deadzone_tilt) / (0.5f - deadzone_tilt + 1e-6f);
            if (ratio > 1.0f) ratio = 1.0f;
            if (ratio < 0.0f) ratio = 0.0f;
            d_tilt *= (c.slow_gain + (1.0f - c.slow_gain) * sqrtf(ratio));
        }
    }

    // clamp 必须分轴：tilt 若仍用 max_step_pan(0.6)，单帧 0.6×80 = 48px 仍会冲出 32px
    // 死区的 1.5 倍 → 极限环，这正是"蹲下站起来云台上下摆好几轮"的根因。
    d_pan  = clampf(d_pan,  -max_step_pan,  max_step_pan);
    d_tilt = clampf(d_tilt, -max_step_tilt, max_step_tilt);

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

    // ── tilt 安全区间硬钳位（2026-11-16）──────────────────────────────
    // 放在 pan_range / tilt_range 钳位**之后**，顺序不能反：
    //   tilt_range(±45°) 是"别撞坏机械"的兜底，tilt_safe([-35,-5]) 是
    //   "别对着天花板/地板"的画面兜底，后者更窄，必须最后生效。
    // 之前出现过 tilt 被推到限幅、镜头朝天、画面里一个人都没有 →
    // 检测不到人 → 更不动作 → 永远回不来。这个钳位就是防这个死锁。
    //
    // ★ 只在 enable_tilt=true 时生效，这条别去掉：
    //   tilt 的安全区间是 [-35,-5]，**不包含 0**（0 是固件回中位，镜头偏平）。
    //   若对 --no-tilt 也钳位，cur_tilt 会从初值 0 被拽到 -5，
    //   于是 HUD / /status 显示 -5、下发给固件的也是 -5 —— 而 --no-tilt 的本意是
    //   "tilt 舵机没接、保持不动"。那会凭空改变启动行为（start_with_bridge.sh
    //   就是用 --no-tilt 起的），所以这里显式排除。tilt 关掉时它本来也不会漂。
    if (c.enable_tilt) {
        out_tilt = clampf(out_tilt, c.tilt_safe_min, c.tilt_safe_max);
    }
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

// letterbox：原版（每次都新建 Mat）。保留给需要独立 dst 的调用方，
// 流水线里改用下面的 letterbox_into()，避免每帧分配。
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

// letterbox 的**复用版**：往一个已经分配好的 dst 里写，不新建 Mat。
//
// 为什么需要它（流水线的内存约束）：
//   原版每帧要做 3 次分配：
//       ① cv::Mat resized（缩放结果，640x360x3 = 0.66MB）
//       ② dst = Mat(640,640,CV_8UC3)（1.2MB，**整块 memset 成 114**）
//       ③ resized.copyTo(dst(roi))
//   30 FPS 下就是每秒 36MB 的分配/释放。流水线里 dst 由 Slot 持有、跨帧复用，
//   只有 ① 那点 0.66MB 还是每帧分配（resize 必须要有独立输出）。
//   想彻底消掉 ①，得改成 resize(src, dst_roi, ...) 直接写到 ROI 上；
//   但 ROI 的 addr 跨度 != dst.cols*3，OpenCV 要按 step 走，实测反而更慢
//   （会退化成带 stride 的逐行拷贝）。所以这里保留 resized，只省掉 ②③。
//
// 为什么 ② 用 dst.setTo() 而不用 `dst = Mat(...)`：
//   `dst = Mat(size,size,type,Scalar)` 会先释放旧 buffer 再重新分配，
//   等于把复用扔掉了。setTo 只写像素不换 buffer。
static void letterbox_into(const cv::Mat& src, cv::Mat& dst, cv::Mat& resized_buf,
                            int size, float& scale, int& pad_x, int& pad_y) {
    const int w = src.cols, h = src.rows;
    scale = std::min((float)size / w, (float)size / h);
    const int nw = (int)(w * scale), nh = (int)(h * scale);

    cv::resize(src, resized_buf, cv::Size(nw, nh), 0, 0, cv::INTER_LINEAR);

    // 只在第一次（或尺寸变化时）真正分配 dst
    if (dst.rows != size || dst.cols != size || dst.type() != src.type()) {
        dst.create(size, size, src.type());
    } else {
        dst.setTo(cv::Scalar(114, 114, 114));   // 只填边距，不换 buffer
    }
    pad_x = (size - nw) / 2;
    pad_y = (size - nh) / 2;
    resized_buf.copyTo(dst(cv::Rect(pad_x, pad_y, nw, nh)));
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

// 摄像头朝向矫正角度：0（默认）/ 90 / 180 / 270，单位度，逆时针。
// 摄像头倒装时用 180。放在全局是因为主循环每帧都要读它，
// 传参进去会让本来就很长的 main 再多一层参数。
static int g_rotate = 0;

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

// ============================================================================
//  流水线并行基础设施（2026-11-16 串行 → 三阶段流水线）
// ============================================================================
//
//  为什么改
//  --------
//  改之前主循环是完全串行的：采集 8.8 + 预处理 2.2 + NPU 16.6 + 解码 1.3
//  + 画框 2.5 + JPEG 7.4 = 38.9ms/帧（26 FPS）。这六段里有三段用的是**完全
//  不同的硬件**：
//      · 采集  ── USB 总线 / uvcvideo
//      · NPU   ── RK3588 的 NPU（不占 CPU Core）
//      · 画框 JPEG ── CPU
//   串行时它们在互相等，并行时本可以重叠。所以拆成三个阶段：
//
//      Thread A (采集+预处理)  →  Q1  →  Thread B (NPU+解码+控制)  →  Q2  →  Thread C (画框+JPEG)
//
//  理论帧率 = 1 / max(A, B, C)（各阶段耗时见 main 里 pipeline_*_ms 的实测）
//
//  ★ 一个必须先说清楚的现实约束（否则下面的"没到 58 FPS"会被误读成实现失败）
//  ------------------------------------------------------------------------
//  这颗 UVC 摄像头在 1280x720 下**硬件上限就是 30 FPS**（v4l2-ctl 报
//  Interval 0.033s，实测 read() 恰好 30.00 fps、帧间隔 33.33ms）。
//  所以流水线的上限是 min(1/max(A,B,C), 30)，**不可能**超过 30 FPS。
//  串行版 26 FPS 掉到 30 以下是**因为 CPU 阶段拖累了取流**：
//  uvcvideo 只有 4 个 mmap buffer，主循环 39ms 一轮就等于 39ms 才 QBUF 一次，
//  相机 FIFO 满 → 丢帧。流水线把取流交给专用线程后，CPU 才不再是取流的上限。
//  详细拆解实验见报告。
//
//  为什么队列容量是 1 且"丢旧"
//  ---------------------------
//  追踪要的是**当前时刻**的目标位置。如果 Q1 里积压了 5 帧，Thread B 处理完
//  一帧后拿到的还是 5 帧前的老画面，云台就会在追"过去"——而且每一帧都追一个
//  不同的过去，表现为云台抖。所以：
//      · 队列容量 = 1
//      · push 时直接覆盖（= 丢最旧的）→ B 永远拿到最新帧
//  这不是"怕内存溢出"的妥协，而是追踪语义的要求。
//
//  为什么 Mat 要跑一个**固定大小**的对象池（而不是每帧 new/delete）
//  ----------------------------------------------------------------
//  1280x720x3 = 2.7MB/帧。30 FPS 下每帧分配+释放一次，即使有 glibc 的
//  tcache 帮忙，也仍然有 memset、mmap 抖动和 RSS 上涨的风险。
//  这里用固定 3 块 Slot 的池（为什么是 3 块、为什么固定不扩容，
//  见下面 MatSlot / SlotPool 的注释）：稳态**零 malloc**，RSS 是平的。
//  总内存是**常数** 15.3MB（3 × (2.7 bgr + 1.2 lb + 1.2 rgb + 1.2 rz)），
//  不随帧数增长 —— 这是本项目对"每帧 2.7MB 会不会内存暴涨"的回答。
//
//  为什么控制逻辑从 C 提前到 B
//  ---------------------------
//  控制看的是"目标此刻在哪"，最不该等显示。放在 B（NPU 一出结果就算）后：
//      控制延迟 = A + B      （画框/JPEG 完全不参与）
//  放在 C 的话控制延迟 = A + B + C，而且 C 一慢云台就滞后。
//  画框、BEV 雷达、HUD、JPEG 编码这些纯显示工作留在 C：它们慢只影响网页
//  刷新率，不影响云台。这是本设计最关键的取舍。
//
// ---------------------------------------------------------------------------

#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>

// ---- 1. 有界单元素队列（满了丢旧）------------------------------------------
//
// ★ 为什么不用 std::queue + 判满就丢新：
//   丢"新"等于 B 总是处理最老的那帧，延迟会越积越大；
//   覆盖旧值才能在 B 变慢时自动"跟到最新"。追踪场景下后者才是对的。
//
// ★ 关队列（close）与退出（g_running=0）是两条路，都必须能唤醒等待者：
//   只判 g_running 的话，push 端已经退出的情况下消费者会永远等下去
//   （没有任何人会再 notify）。所以 close() 里要 notify_all。
template <typename T>
class LatestQueue {
public:
    // 推入一项。队列里已有的旧值被**直接覆盖掉**（丢旧，见上面注释）。
    //
    // ★ drop_counter 参数：覆盖发生时把计数 +1，用于诊断"到底丢了多少帧"。
    //   这是本任务必须有的指标 —— 没有它就无法区分"流水线跑得好"和
    //   "B 慢得离谱、C 一直吃旧帧但帧率看着还行"。
    //   传的是 std::atomic<uint64_t>*，为 nullptr 时不计数。
    void push(T&& v, std::atomic<uint64_t>* drop_counter = nullptr) {
        bool dropped = false;
        {
            std::lock_guard<std::mutex> lk(m_);
            if (closed_) return;
            dropped = has_;              // 原来就有一项 → 这次是覆盖
            item_ = std::move(v);
            has_ = true;
        }
        if (dropped && drop_counter) (*drop_counter)++;
        cv_.notify_one();
    }

    // 取一项。返回 false 表示"该结束了"（被 close 或 g_running 归零）。
    //
    // ★ 这里刻意**不**把 running 传成模板参数去做 while 判断，而是在
    //   wait 里同时等 has_ || closed_，退出靠 close() 唤醒 + pred 复查。
    //   原因：g_running 是 volatile sig_atomic_t，它**不会**触发条件变量的
    //   任何通知；只靠 `while (!has_ && running)` 等待时，若信号恰好发生在
    //   判完 pred、进入 wait 之前，那个 wait 就永远醒不过来了
    //   （经典的 lost-wakeup）。所以主线程退出时必须显式 close() 两个队列。
    bool pop(T& out) {
        std::unique_lock<std::mutex> lk(m_);
        cv_.wait(lk, [&] { return has_ || closed_; });
        if (closed_ || !has_) return false;
        out = std::move(item_);
        has_ = false;
        return true;
    }

    // 关队列：唤醒所有等待者，之后 push 无效、pop 立即返回 false。
    void close() {
        {
            std::lock_guard<std::mutex> lk(m_);
            closed_ = true;
        }
        cv_.notify_all();
    }

private:
    std::mutex m_;
    std::condition_variable cv_;
    T item_{};
    bool has_ = false;
    bool closed_ = false;
};

// ---- 2. 帧对象 Slot + 固定大小对象池（bgr 双缓冲）--------------------------
//
// 生命周期（单向一条链，不回头）：
//      池 --acquire--> A(采集+预处理) --push--> Q1 --pop--> B(NPU+控制)
//         --push--> Q2 --pop--> C(画框+JPEG) --release--> 池
//
// ★ 为什么至少 3 块、而不是 1 块（这是"不能改回单块"的关键）：
//   A 抓完第 N 帧交给 B 之后，必须能**立刻**抓第 N+1 帧，否则 A 被 B 拖住，
//   流水线就退化成串行（A 的采集/预处理和 B 的 NPU 完全不重叠了）。
//   稳态下同一时刻有三帧在流水线里：A 手上 1 帧、B 手上 1 帧、C 手上 1 帧，
//   所以池里至少要有 3 块。再少就会让后面的阶段把前面的阶段卡住。
//
// ★ 为什么池**必须固定大小 + 拿不到就阻塞**（而不是"拿不到就新建"）：
//   如果拿不到就新建，一旦 C 变慢（比如网页端卡住），池会无限膨胀
//   —— 每个 Slot 是 2.7+1.2+1.2 = 5.1MB，几秒钟就能吃掉几百 MB。
//   固定大小 + 阻塞 = 天然的背压：C 慢 → A 拿不到块 → A 不再取流 →
//   相机自己的缓冲溢出丢帧。这是**有界**退让，不会爆内存。
//   实测 A 采集+预处理 11ms < B 17ms，所以稳态下 A 几乎不用等。
//
// ★ 为什么 Slot 里 bgr / lb / rgb 三块要放在同一个对象里（不拆开）：
//   它们生命周期虽然有长短之分（lb/rgb 只用完 B 就用完，bgr 要活到 C 画完），
//   但**拆开管理带来的复杂度换不来收益**：固定 3 块时 bgr 共 8.1MB、
//   lb/rgb 共 7.2MB，总共 15.3MB，是个常数，不会涨。
//   而如果拆开，A 就需要"bgr 池 + lb/rgb 池"两套获取逻辑，
//   任何一处漏还会变成跨线程覆盖正在使用的内存 —— 不值得。
//   记住这个数：**总内存是常数 15.3MB**，这才是重点，不是"省到 10.x"。
//
// ★ 为什么回池时**不** Mat::release()：
//   release() 会把引用计数清零、内存还给分配器，下一帧 read/resize 又得
//   重新 malloc 2.7MB —— 那就成了"每帧 2.7MB 分配"，正是要避免的。
//   已实测：cap.read() 读进一个"已有内容的 Mat"时 100% 复用原 buffer 指针，
//   letterbox 的 resize/copyTo/cvtColor 也都复用，所以保留 buffer
//   就能做到**稳态零 malloc**（报告里有 probe 数据）。
struct MatSlot {
    cv::Mat cap;             // A 的采集落点（相机原始尺寸 1280x720x3）
    cv::Mat bgr;             // 交给 C 画的工作图 = cap（rotate=0 时）或 rotate 结果
    cv::Mat lb;              // A 写（letterbox 640x640），B 读做 NPU 输入
    cv::Mat rgb;             // A 写（BGR→RGB），B 读做 NPU 输入
    cv::Mat rz;              // A 的 letterbox 中间层（复用，避免每帧 resize 分配）
    cv::Mat rot;             // A 的 rotate 中间层（复用）
    float scale = 1.f;       // letterbox 缩放比（检测框逆变换要用）
    int pad_x = 0, pad_y = 0;
    int64_t t_capture_us = 0;   // 相机把这一帧交出来的时刻（量端到端延迟用）
    uint32_t frame_id = 0;
};

// ---- 流水线的诊断计数器（定义在使用它们的类之前）--------------------------
//
// ★ 为什么这些必须是全局的：SlotPool / LatestQueue 是模板/工具类，
//   它们要知道"丢了多少帧""池里几块在用"，但不该各自持有一份计数
//   （那样每帧打印时要挨个问，且数量对不上）。
//   全部用 std::atomic —— 只有一个线程写、一个线程读，但读写并发必须是无锁原子的。
static std::atomic<uint64_t> g_pool_in_use{0};  // 当前被流水线持有的 Slot 数
static std::atomic<uint64_t> g_q1_drop{0};      // Q1 覆盖掉旧帧的次数（= 丢帧）
static std::atomic<uint64_t> g_q2_drop{0};      // Q2 覆盖掉旧帧的次数
static std::atomic<uint64_t> g_a_frames{0};     // A 抓到的帧数
static std::atomic<uint64_t> g_b_frames{0};     // B 推理完的帧数
static std::atomic<uint64_t> g_c_frames{0};     // C 画完的帧数
static std::atomic<uint64_t> g_ctrl_sent{0};    // B 实际下发云台指令的次数

// 空闲 Slot 池。acquire 可以阻塞（有界背压，见上），但必须能被 close 唤醒。
class SlotPool {
public:
    explicit SlotPool(size_t n) {
        for (size_t i = 0; i < n; ++i) slots_.push_back(new MatSlot());
    }
    ~SlotPool() { for (auto* s : slots_) delete s; }
    SlotPool(const SlotPool&) = delete;
    SlotPool& operator=(const SlotPool&) = delete;

    // 取一块空闲的。返回 nullptr = 池已关闭，该退出线程了。
    //
    // ★ 这里用 `cv_.wait(lk, pred)` 而不是 `while(!pred) cv_.wait(lk)`：
    //   两者语义等价，但 wait(pred) 已经把"判谓词 + 睡"做成原子操作，
    //   天然免疫 lost-wakeup。退出仍然靠 main 显式 close() 来通知，
    //   因为 g_running 是 volatile sig_atomic_t，它无法唤醒条件变量。
    MatSlot* acquire() {
        std::unique_lock<std::mutex> lk(m_);
        cv_.wait(lk, [&] { return !slots_.empty() || closed_; });
        if (closed_) return nullptr;
        MatSlot* s = slots_.front();
        slots_.pop_front();
        g_pool_in_use++;
        return s;
    }

    void release(MatSlot* s) {
        if (!s) return;
        {
            std::lock_guard<std::mutex> lk(m_);
            slots_.push_back(s);
            g_pool_in_use--;
        }
        cv_.notify_one();
    }

    void close() {
        {
            std::lock_guard<std::mutex> lk(m_);
            closed_ = true;
        }
        cv_.notify_all();
    }

private:
    std::deque<MatSlot*> slots_;
    std::mutex m_;
    std::condition_variable cv_;
    bool closed_ = false;
};

// ---- 3. 阶段耗时统计 -------------------------------------------------------
//
// ★ 为什么统计要按阶段分开、还要量"端到端延迟"：
//   流水线的帧率由**最慢的那一段**决定，不是各段相加；所以光看"合计"会误判。
//   同时并行之后"各段相加"不再等于帧间隔，必须实测端到端延迟
//   （A 抓到 → B 下发控制），否则报告里"控制延迟 = A+B"就只是算出来的。
//
// ★ 用 mutex 而不是 std::atomic<double>：
//   atomic<double> 没有 fetch_add（C++20 才有），`sum += x` 实际是 load/add/store，
//   不是原子读改写。虽然每段只有一个线程写、只有 C 线程读，多线程下仍是数据竞争
//   （UB）。这里每次 add 只加一次锁（~20ns），60 帧才读一次，完全不影响性能。
struct StatAcc {
    double sum = 0.0;
    int    n = 0;
    void add(double ms) {
        std::lock_guard<std::mutex> lk(m_);
        sum += ms;
        n++;
    }
    double avg() {
        std::lock_guard<std::mutex> lk(m_);
        return n ? sum / n : 0.0;
    }
    int count() {
        std::lock_guard<std::mutex> lk(m_);
        return n;
    }
    void reset() {
        std::lock_guard<std::mutex> lk(m_);
        sum = 0.0;
        n = 0;
    }
private:
    std::mutex m_;
};

static StatAcc g_st_capture;    // A: 采集（cap.read，含等相机的 25ms）
static StatAcc g_st_pre;        // A: 预处理（rotate + letterbox + cvtColor）
static StatAcc g_st_npu;        // B: NPU 推理
static StatAcc g_st_decode;     // B: DFL 解码
static StatAcc g_st_ctrl;       // B: 控制计算 + 下发
static StatAcc g_st_draw;       // C: 画框 + BEV + HUD
static StatAcc g_st_jpeg;       // C: JPEG 编码
static StatAcc g_st_latency;    // A 抓到 → B 下发控制（端到端，实测）
static StatAcc g_st_periodA;    // A 相邻两帧的间隔（= 实际采集帧率）
static StatAcc g_st_periodC;    // C 相邻两帧的间隔（= 实际出图帧率）

static inline int64_t now_us() {
    return std::chrono::duration_cast<std::chrono::microseconds>(
               std::chrono::steady_clock::now().time_since_epoch()).count();
}
static inline double us_to_ms(int64_t us) { return (double)us / 1000.0; }

// ============================================================================
//  三阶段流水线的线程函数（Thread A / B / C）
// ============================================================================
//
//  ★ 先看这张数据流图，后面每个线程的注释都在解释它的一部分：
//
//      [相机 30FPS]
//           ↓ SlotPool.acquire()
//      ┌──────────────────────────┐
//      │ Thread A: 采集 + 预处理   │  采集 8.8 + 旋转 + letterbox 2.2 ≈ 11ms
//      │  cap.read → rotate → lb  │  （其中 8.8ms 里绝大部分是**等相机**）
//      └──────────────────────────┘
//           ↓ Q1.push（容量1，丢旧）
//      ┌──────────────────────────┐
//      │ Thread B: NPU + 解码 + 控制│  NPU 16.6 + 解码 1.3 + 控制 0.2 ≈ 18ms
//      │  rknn_run → decode → 控制 │  ★ 控制在这里下发，不等显示
//      └──────────────────────────┘
//           ↓ Q2.push（容量1，丢旧）
//      ┌──────────────────────────┐
//      │ Thread C: 画框 + JPEG     │  画框 2.5 + JPEG 7.4 ≈ 10ms
//      │  画框 → HUD → imencode    │  ★ 纯显示，慢了不影响云台
//      └──────────────────────────┘
//           ↓ SlotPool.release()
//
//  三阶段各跑各的线程，帧在阶段之间流动。稳态下每帧的"节拍"由**最慢的阶段**决定。
//
// ---------------------------------------------------------------------------

// 流水线里流动的一帧"元数据"。
// ★ 为什么只传指针 + 少量标量，而不是把 Mat 拷进去：
//   Mat 的拷贝是浅拷贝（引用计数），听着没事；但一拷贝就会有 3 个 Mat
//   同时指着同一块像素，而"谁负责最后 release"就说不清了。
//   这里干脆只传 Slot*，**所有权完全由 SlotPool 的 acquire/release 表达**，
//   任何时刻一块内存只属于一个阶段，不可能出现两个线程同时写同一块。
//   （唯一的共享只读是 B 交给 C 的 bgr —— B 交出去之后就不再碰它。）
struct FrameItem {
    MatSlot* slot = nullptr;
    int64_t  t_capture_us = 0;   // 端到端延迟起点
    uint32_t frame_id = 0;
};

// 队列容量固定为 1：见文件里 LatestQueue 的注释（追踪只要最新帧）。
using FrameQ = LatestQueue<FrameItem>;

// Thread B 交给 Thread C 的推理结果。
// ★ 为什么检测框要在这里深拷贝一份（而不是让 C 重新算/共享 dets）：
//   dets 是 B 的局部变量，出栈就没了；而 C 要拿它画框。
//   std::vector<Detection> 的拷贝是几千字节量级（一帧一般 0~10 个框），
//   相比 2.7MB 的图像完全不是瓶颈，换来的是**B 和 C 之间没有共享可变状态**。
//   这是有意的：跨线程共享可变容器是最容易埋数据竞争的地方，宁可拷。
struct InferResult {
    MatSlot* slot = nullptr;
    std::vector<Detection> dets_in_orig;              // 已逆变换到原图坐标
    int64_t  t_capture_us = 0;
    uint32_t frame_id = 0;

    // B 算好的控制结果，C 只用于显示 HUD。
    // ★ 注意：这**不是**控制指令的来源，只影响 HUD 上那行字。
    //   真正下发给云台的动作已经在 B 里做完了 —— 别在 C 里再下发一次，
    //   那会让同一帧控制两次，破坏控制律（compute_control 是增量式的，
    //   一帧算两次 = 步长翻倍，会直接毁掉 2026-11-16 修好的 tilt 防振设计）。
    float cur_pan = 0.f, cur_tilt = 0.f;
    bool  locked = false;
    bool  has_target = false;
    int   rejected_partial = 0;
    // 目标点在原图上的归一化坐标（C 用它画绿色十字准星）
    float tgt_cx = 0.5f, tgt_cy = 0.5f, tgt_frac_w = 0.f;
    const char* state_str = "LOST";
    double npu_ms = 0.0, dec_ms = 0.0;
};

// B → C 的队列类型（容量 1，丢旧，理由同 Q1）
using InferQ = LatestQueue<InferResult>;

// ---- 阶段耗时打印 ----------------------------------------------------------
// 每 60 帧打一行。★ 这是本任务"帧率有没有真的提上去"的**唯一可信来源**，
// 不要靠肉眼看网页卡不卡来判断。
//
// ★ 为什么 fps 要按"窗口内的帧数 / 窗口时长"算，而不是 frame_id / elapsed：
//   ① 原来的串行版用 frame_id / (now - t_start) 是**全程平均**，
//      启动阶段的慢帧会一直拉低它，看不出稳态值。
//   ② 流水线里 A/B/C 各有各的帧率，必须各自单独量。
//      而且 A 的帧率才是"相机给我们多少"，C 的帧率是"网页看到多少"——
//      两者不同就说明中间在丢帧。
//   ③ 窗口时长要用**两次打印之间**的真实间隔，不能假设是 60 帧 × 17ms。
static int64_t g_prof_last_us = 0;
static uint64_t g_prof_last_a = 0, g_prof_last_c = 0;
// 最近一帧的控制状态，供 profile 行打印（用于和串行版对账"云台还在正常工作"）
static std::atomic<float>    g_prof_pan{0.f};
static std::atomic<float>    g_prof_tilt{0.f};
static std::atomic<int>      g_prof_state{0};     // 0=LOST 1=TRACK 2=LOCK 3=SATURATED 4=NO_GOOD_TGT
static std::atomic<int>      g_prof_rejected{0};

static const char* state_name(int s) {
    switch (s) {
    case 1: return "TRACK";
    case 2: return "LOCK";
    case 3: return "SATURATED";
    case 4: return "NO_GOOD_TGT";
    default: return "LOST";
    }
}

static void print_pipeline_profile() {
    int64_t t_now = now_us();
    if (g_prof_last_us == 0) {
        // ★ 第一次调用只登记基线，**不打印**。
        //   否则窗口时长是 0（或半个窗口），算出来的 fps 会是 58.6 这种
        //   看着很美、实际是"半窗时间里有 60 帧"的假值。
        g_prof_last_us = t_now;
        g_prof_last_a = g_a_frames.load();
        g_prof_last_c = g_c_frames.load();
        g_st_capture.reset(); g_st_pre.reset(); g_st_npu.reset(); g_st_decode.reset();
        g_st_ctrl.reset(); g_st_draw.reset(); g_st_jpeg.reset();
        g_st_latency.reset(); g_st_periodA.reset(); g_st_periodC.reset();
        return;
    }
    const double win_s = us_to_ms(t_now - g_prof_last_us) / 1000.0;
    if (win_s <= 0.01) return;                   // 窗口太短，等下一次

    const double a_ms = g_st_capture.avg() + g_st_pre.avg();
    const double b_ms = g_st_npu.avg() + g_st_decode.avg() + g_st_ctrl.avg();
    const double c_ms = g_st_draw.avg() + g_st_jpeg.avg();
    const double slowest = std::max(a_ms, std::max(b_ms, c_ms));

    // ★ 用**本窗口内**新增的帧数算 fps —— 这才是这一段时间的真实帧率
    const uint64_t a_now = g_a_frames.load(), c_now = g_c_frames.load();
    const double a_fps = (a_now - g_prof_last_a) / win_s;
    const double c_fps = (c_now - g_prof_last_c) / win_s;
    g_prof_last_a = a_now;
    g_prof_last_c = c_now;

    // ★ 这一行的格式：先给"原来那 6 段"的分解（便于和串行版逐项对账），
    //   再给 A/B/C 三阶段的和（这是流水线的节拍来源），最后给实测帧率。
    printf("[profile] 采集 %5.1f | 预处理 %4.1f | NPU %5.1f | 解码 %4.1f | 画框 %4.1f | JPEG %5.1f"
           " || A %5.1f | B %5.1f | C %5.1f | 最慢 %5.1f ms(理论 %.1f fps)"
           " || 采集 %.1f fps 出图 %.1f fps"
           " || 延迟 %.1f ms | Q1丢 %llu Q2丢 %llu | SLOT %llu"
           " | P%+.1f T%+.1f %s 拒绝框 %d 下发 %llu\n",
           g_st_capture.avg(), g_st_pre.avg(), g_st_npu.avg(), g_st_decode.avg(),
           g_st_draw.avg(), g_st_jpeg.avg(),
           a_ms, b_ms, c_ms, slowest, slowest > 0 ? 1000.0 / slowest : 0.0,
           a_fps, c_fps,
           g_st_latency.avg(),
           (unsigned long long)g_q1_drop.load(), (unsigned long long)g_q2_drop.load(),
           (unsigned long long)g_pool_in_use.load(),
           g_prof_pan.load(), g_prof_tilt.load(), state_name(g_prof_state.load()),
           g_prof_rejected.load(),
           (unsigned long long)g_ctrl_sent.load());
    fflush(stdout);

    g_st_capture.reset(); g_st_pre.reset(); g_st_npu.reset(); g_st_decode.reset();
    g_st_ctrl.reset(); g_st_draw.reset(); g_st_jpeg.reset();
    g_st_latency.reset(); g_st_periodA.reset(); g_st_periodC.reset();
    g_prof_last_us = t_now;
}

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

// 流水线专用的读帧：和 cam_read() 唯一的区别是**不 release dst**。
//
// ★ 为什么这条很重要（内存指标全靠它）：
//   cam_read() 一上来就 `dst.release()`，那是串行版的正确做法 ——
//   它要防"读失败时上一帧留在 dst 里被当成新帧"。
//   但那同时也意味着**每帧都重新 malloc 2.7MB**：release 把引用计数清零、
//   内存还给分配器，下一次 read 又得重新分配。
//   流水线里 dst 是 Slot 里的长寿命 buffer，实测 read() 写进一个
//   "已经有内容"的 Mat 时 100% 复用原 buffer（probe3 结果），
//   所以这里保留 buffer，只靠返回值表达"这一帧有没有坏" —— 
//   坏帧时调用方会 continue，不会拿 dst 去用，安全性一样。
static bool cam_read_keep(cv::VideoCapture& cap, cv::Mat& dst) {
    bool ok = false;
    try {
        ok = cap.read(dst);
    } catch (const cv::Exception& e) {
        g_cam_dropped++;
        g_cam_consec_fail++;
        if (g_cam_dropped <= 5 || (g_cam_dropped % 60) == 0) {
            std::cerr << "[!] 断帧 #" << g_cam_dropped
                      << "（累计重开 " << g_cam_reopens << "）: " << e.what() << "\n";
        }
        return false;
    }
    if (!ok || dst.empty()) {
        g_cam_dropped++;
        g_cam_consec_fail++;
        return false;
    }
    g_cam_consec_fail = 0;
    return true;
}

// ============================================================================
//  三阶段流水线：共享上下文 + 三个线程函数
// ============================================================================
//
//  数据流（★ 读这个文件先看这张图）：
//
//        [相机 30FPS 硬上限]
//             ↓ pool.acquire()
//        ┌────────────────────────────┐
//        │ Thread A  采集 + 预处理      │  read 8.8 + rotate/letterbox/cvt 2.2 ≈ 11ms
//        └────────────────────────────┘
//             ↓ q1.push（容量 1，满了丢旧）
//        ┌────────────────────────────┐
//        │ Thread B  NPU + 解码 + 控制  │  NPU 16.6 + 解码 1.3 + 控制 0.2 ≈ 18ms
//        │  ★ 控制在这里算、在这里下发   │
//        └────────────────────────────┘
//             ↓ q2.push（容量 1，满了丢旧）
//        ┌────────────────────────────┐
//        │ Thread C  画框 + JPEG + 推流 │  画框 2.5 + JPEG 7.4 ≈ 10ms
//        └────────────────────────────┘
//             ↓ pool.release()
//
// ---------------------------------------------------------------------------

// 流水线共享上下文。
// ★ 为什么要把共享量收进一个 struct，而不是继续堆全局变量：
//   本文件原来就有 g_running / g_latest_jpeg / g_status 等全局量。再散着加
//   十几个，"哪些是流水线内部、哪些是对外接口"就彻底看不清了，
//   更没法一眼审计数据竞争。收进来之后每项只有一处定义，谁写谁读一目了然。
//
// ★ 数据竞争审计（新增代码的全部竞态面，逐项核对）：
//   ┌ 共享量            ┬ 并发方式                     ┬ 保护措施
//   ├ q1 / q2           │ A/B/C 三线程                │ LatestQueue 内部 mutex+cv
//   ├ pool              │ A/C 两线程                  │ SlotPool 内部 mutex+cv
//   ├ gimbal            │ B 写（set_target）/HTTP 读    │ GimbalSerial 内部 pthread mutex
//   ├ g_latest_jpeg     │ C 写 / HTTP 读               │ g_jpeg_lock（原有）
//   ├ g_status          │ C 写 / HTTP 读               │ g_status.lock（原有）
//   ├ g_latest_det(9000)│ B 写 / 9000 线程读           │ g_det_lock（原有）
//   ├ g_st_* / g_q*_drop│ 各阶段写 / C 读              │ StatAcc 内锁 / std::atomic
//   └ g_running         │ SIG 处理函数写 / 全线程读     │ volatile sig_atomic_t
//   ⇒ 除以上，**没有**未受保护的跨线程可变变量。
struct PipelineCtx {
    // ── main 填好之后只读 ──────────────────────────────────────────────
    cv::VideoCapture* cap = nullptr;
    const char* dev_path = nullptr;
    bool is_video_file = false;
    rknn_context ctx = 0;
    std::vector<rknn_output>* outputs = nullptr;
    int  bi = -1, ci = -1;
    bool bnhwc = false, cnhwc = false;
    ControlCfg cfg;
    bool gimbal_on = false;

    // ★ 云台对象的指针（不是副本！）。
    //   gimbal 是 main 的局部对象，线程函数访问不到它，只能通过指针。
    //   之所以传指针而不是把 GimbalSerial 塞进 PipelineCtx，是因为
    //   GimbalSerial 内有 pthread_mutex_t/cond_t 且不可拷贝（原设计如此），
    //   塞进来就要在某处构造一个，反而多一份生命周期要管。
    //   安全性：GimbalSerial::set_target/state 内部都自己加锁，多线程调用是安全的。
    GimbalSerial* gimbal = nullptr;

    // ── 流水线设施 ────────────────────────────────────────────────────
    FrameQ*  q1 = nullptr;      // A → B
    InferQ*  q2 = nullptr;      // B → C
    SlotPool* pool = nullptr;

    // ── 由 A 写、B/C 读的帧号（原子，因为 A 写、C 的 HUD 也要读）──────
    std::atomic<uint32_t> frame_id{0};
};

// ---- Thread A：采集 + 预处理 -----------------------------------------------
//
// 职责：相机的裸帧 → (a) C 能画的 bgr  (b) NPU 能吃的 640x640 RGB
//
// ★ 为什么"等相机"必须单独占一个线程（这是整个优化的收益来源）：
//   实测 cap.read() 8.8ms，但拆开是 grab() 25ms + retrieve() 8.2ms ——
//   那 25ms 里线程**什么也没干，纯粹在等 uvcvideo 的 select()**。
//   串行版把这段等待算进了每帧预算，所以 39ms 里有 1/3 是在干等。
//   单独一个线程等相机，等的过程中 B/C 在干活，这就是并行的全部收益。
//   （注：相机只有 30FPS，A 的真实节拍是 33ms 一帧，A 自己的 CPU 活只有 2~3ms。）
//
// ★ 为什么 rotate/letterbox/cvtColor 放在 A 而不是 B：
//   它们是纯 CPU 活，跟 B 的 NPU 完全不抢资源（NPU 不占 CPU 核）。
//   放在 A，B 一拿到就能立刻 rknn_run，B 的 17ms 里没有 CPU 尾巴；
//   放到 B 会把 B 拉长到 19ms —— 而 B 恰恰是最慢的一段，绝不能给它加活。
static void* thread_a_capture(void* arg) {
    PipelineCtx* px = static_cast<PipelineCtx*>(arg);
    int64_t t_prev_cap = 0;

    while (g_running) {
        // ---- 1. 从池里拿一块空闲 Slot ----
        // ★ 这里可能阻塞（3 块都在外面时）。这是**有意的背压**：
        //   C 慢 → 块回不来 → A 停下不再取流 → 相机自己丢帧。
        //   有界退让，不会像"拿不到就新建"那样把内存吃爆。
        MatSlot* s = px->pool->acquire();
        if (!s) break;                       // 池已关闭 → 退出

        // ---- 2. 采集 ----
        auto tA0 = std::chrono::steady_clock::now();
        if (!cam_read_keep(*px->cap, s->cap)) {
            // 坏帧/断帧：把块还回去，按原来的策略重试或重开
            px->pool->release(s);
            if (px->is_video_file) {
                px->cap->set(cv::CAP_PROP_POS_FRAMES, 0);
                continue;
            }
            if (g_cam_consec_fail >= CAM_REOPEN_AFTER) {
                g_cam_reopens++;
                std::cerr << "[!] 连续 " << g_cam_consec_fail << " 帧读不出来，第 "
                          << g_cam_reopens << " 次重开 " << px->dev_path << "\n";
                g_cam_consec_fail = 0;
                if (!open_camera(*px->cap, px->dev_path, false, false)) usleep(1000000);
                continue;
            }
            usleep(5000);
            continue;
        }
        auto tA1 = std::chrono::steady_clock::now();
        const int64_t t_cap = now_us();

        const uint32_t fid = ++px->frame_id;
        s->frame_id = fid;
        s->t_capture_us = t_cap;

        // ---- 3. 摄像头朝向矫正（--rotate）----
        //
        // 原文的推导原样保留：必须在 letterbox **之前**做，因为后面所有几何换算
        // （letterbox 的 scale/pad、检测框逆变换、tgt_cx/tgt_cy、BEV 反投影）
        // 全都基于 bgr 的宽高与坐标系。先转再算，它们自动全部正确。
        //
        // ★ 流水线带来的改进：原来这里 `cv::flip(bgr, rot, -1); bgr = rot;`
        //   每次迭代都新建一个 2.7MB 的 rot。现在 rot 是 Slot 里的长寿命 buffer，
        //   且 180° 用**原地 flip**（实测原地与非原地结果完全一致，
        //   见 probe3 结果），稳态零分配。
        cv::Mat& bgr = s->bgr;
        if (g_rotate != 0) {
            if (g_rotate == 180) {
                cv::flip(s->cap, bgr, -1);          // 目标 bgr 复用 Slot 的 buffer
            } else if (g_rotate == 90) {
                cv::transpose(s->cap, s->rot);
                cv::flip(s->rot, bgr, 0);
            } else if (g_rotate == 270) {
                cv::transpose(s->cap, s->rot);
                cv::flip(s->rot, bgr, 1);
            } else {
                s->cap.copyTo(bgr);
            }
        } else {
            // ★ 不旋转时**不做拷贝**，直接让 bgr 指向 cap。
            //   这是 Mat 的浅拷贝，两者共用同一块像素；谁都不会去改它
            //   （A 已经把 cap 填完了，C 只读），所以安全。
            //   省掉一次 2.7MB 的全画面 memcpy。
            bgr = s->cap;
        }

        // ---- 4. letterbox + cvtColor ----
        letterbox_into(bgr, s->lb, s->rz, 640, s->scale, s->pad_x, s->pad_y);
        cv::cvtColor(s->lb, s->rgb, cv::COLOR_BGR2RGB);
        auto tA2 = std::chrono::steady_clock::now();

        g_st_capture.add(std::chrono::duration<double, std::milli>(tA1 - tA0).count());
        g_st_pre.add(std::chrono::duration<double, std::milli>(tA2 - tA1).count());
        if (t_prev_cap) g_st_periodA.add(us_to_ms(t_cap - t_prev_cap));
        t_prev_cap = t_cap;

        // ---- 5. 交给 B（容量 1，若 B 还在忙就覆盖旧的 = 丢旧）----
        // ★ 丢旧是正确的：追踪要的是"此刻"，让 B 去补一帧 100ms 前的画面
        //   只会让云台追一个过期的位置。丢一帧的代价是这一帧不做检测，
        //   而拿旧帧的代价是**做一次错误方向的检测**——后者更糟。
        px->q1->push(FrameItem{s, t_cap, fid}, &g_q1_drop);
        g_a_frames++;
    }
    return nullptr;
}

// ---- Thread B：NPU + 解码 + 控制下发 ---------------------------------------
//
// ★ 控制为什么在 B 而不在 C（本设计最关键的取舍，改代码前读三遍）：
//    控制看的是"目标此刻在哪"，最不该等显示。
//       放 C：控制延迟 = A + B + C ≈ 39ms（等于没优化延迟，只提了帧率）
//       放 B：控制延迟 = A + B     ≈ 30ms，且和 C **完全解耦**
//    更关键的是稳定性：C 要编码 7ms 的 JPEG、还要往 socket 写 100KB 数据，
//    万一网页客户端多或网络卡，C 会抖。控制放在 C 的话，这个抖动会
//    直接变成云台的抖动。放到 B 之后，C 无论多慢，云台节拍只由 B 决定。
//
// ★ 顺序：先 rknn_run → decode → **立刻算控制并下发** → 再把显示数据打包给 C。
//    中间不插入任何画图/编码动作，确保"下发"不被显示拖慢。
static void* thread_b_infer(void* arg) {
    PipelineCtx* px = static_cast<PipelineCtx*>(arg);

    // 云台当前角度：跟踪**最后一次确认**的角度，不是想要的角度（沿用原语义）
    //
    // ★ 2026-11-17 修正：初值必须等于**启动姿态**，不能写死 0。
    //   踩过的坑：加了 --start-pan/--start-tilt 后，姿态确实摆过去了
    //   （日志有 "[+] 启动姿态: P +5.0 -20.0"），但控制律以为当前还在 (0,0)，
    //   第一帧就按"从 0 出发"算增量，结果 tilt 被 tilt_safe 钳到 -5：
    //       P-1.6 T-5.0 LOST     ← 摆到 -20 了却往 -5 修正
    //   所以这里要跟着启动姿态走，否则白摆。
    float cur_pan  = isnan(px->cfg.start_pan)  ? 0.f : px->cfg.start_pan;
    float cur_tilt = isnan(px->cfg.start_tilt) ? 0.f : px->cfg.start_tilt;
    bool locked = false;
    int lost_frames = 0;
    // 低通滤波状态 + settle 暂停（原样保留，语义见原注释）
    float sm_cx = 0.5f, sm_cy = 0.5f;
    bool sm_init = false;
    int jump_skipped = 0;
    uint32_t settle_until_frame = 0;

    std::vector<Detection> dets;
    FrameItem item;

    while (g_running) {
        if (!px->q1->pop(item)) break;
        MatSlot* s = item.slot;
        if (!s) continue;

        // ---- 1. NPU 推理 ----
        rknn_input in{};
        in.index = 0;
        in.buf = s->rgb.data;
        in.size = 640 * 640 * 3;
        in.type = RKNN_TENSOR_UINT8;
        in.fmt  = RKNN_TENSOR_NHWC;

        auto t0 = std::chrono::steady_clock::now();
        rknn_inputs_set(px->ctx, 1, &in);
        rknn_run(px->ctx, nullptr);
        for (auto& o : *px->outputs) { memset(&o, 0, sizeof(o)); o.want_float = 1; }
        rknn_outputs_get(px->ctx, (uint32_t)px->outputs->size(), px->outputs->data(), nullptr);
        auto t1 = std::chrono::steady_clock::now();

        // ---- 2. DFL 解码 ----
        dflfree::decode((const float*)(*px->outputs)[px->bi].buf,
                        (const float*)(*px->outputs)[px->ci].buf,
                        px->bnhwc, px->cnhwc, 640, px->cfg.min_score, 0.45f, dets);
        auto t2 = std::chrono::steady_clock::now();
        rknn_outputs_release(px->ctx, (uint32_t)px->outputs->size(), px->outputs->data());

        const double npu_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();
        const double dec_ms = std::chrono::duration<double, std::milli>(t2 - t1).count();

        // ---- 3. 检测框逆变换 + 找追踪目标（语义与串行版逐字一致）----
        const cv::Mat& bgr = s->bgr;
        const float scale = s->scale;
        const int pad_x = s->pad_x, pad_y = s->pad_y;

        std::vector<Detection> dets_in_orig;
        dets_in_orig.reserve(dets.size());

        float best_area = 0.f;
        float tgt_cx = 0.5f, tgt_cy = 0.5f;
        float tgt_frac_w = 0.f;
        bool has_target = false;
        int rejected_partial = 0;

        const float EDGE_MARGIN = 0.02f;
        const float HEAD_TOP_OFFSET = 0.10f;
        const float ASPECT_MIN = 0.15f;
        const float ASPECT_MAX = 3.00f;

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

            if (d.class_id == 0) {            // person
                const float bw = ox2 - ox1;
                const float bh = oy2 - oy1;
                const float area = bw * bh;

                const bool clip_left  = ox1 <= bgr.cols * EDGE_MARGIN;
                const bool clip_right = ox2 >= bgr.cols * (1.f - EDGE_MARGIN);
                const bool clip_top   = oy1 <= bgr.rows * EDGE_MARGIN;
                const bool clip_bot   = oy2 >= bgr.rows * (1.f - EDGE_MARGIN);
                (void)clip_bot;           // 底边贴边放行：腿被裁不影响头（见原注释）

                const bool head_bad = clip_top || clip_left || clip_right;
                const float aspect = (bh > 1e-3f) ? (bw / bh) : 999.f;
                const bool bad_aspect = (aspect < ASPECT_MIN || aspect > ASPECT_MAX);

                if (px->cfg.filter_partial && (head_bad || bad_aspect)) {
                    rejected_partial++;
                    // ★ 这里**不画框**：画框归 C 干（B 不该碰显示）。
                    //   但"为什么拒"这个信息必须传给 C，否则现场看到
                    //   "检测到了却不追"没法判断是头被切还是长宽比离谱。
                    //   所以把拒绝原因记在 Detection 的 score 里带不过去 ——
                    //   干脆在 C 里按几何重判一次（规则是纯函数，可复现）。
                } else if (area > best_area) {
                    best_area = area;
                    tgt_cx = (ox1 + ox2) * 0.5f / (float)bgr.cols;
                    const float head_off_px =
                        std::min((float)bgr.rows * HEAD_TOP_OFFSET, 0.5f * bh);
                    tgt_cy = (oy1 + head_off_px) / (float)bgr.rows;
                    tgt_frac_w = bw / (float)bgr.cols;
                    has_target = true;
                }
            }
        }

        // ---- 4. 云台控制：算 + **立刻下发** ----
        //
        // ★ 这一段从原主循环里整体搬过来，逻辑一行没改（只把画框的部分
        //   挪到 C 去了）。控制律、分轴参数、settle、滤波、限位全部原样，
        //   所以云台行为与串行版**可逐项对账**。
        auto t3 = std::chrono::steady_clock::now();
        const uint32_t fid = s->frame_id;

        if (px->gimbal_on) {
            if (has_target) {
                lost_frames = 0;

                bool accept = true;
                if (px->cfg.smooth_alpha > 0.f) {
                    if (!sm_init) {
                        sm_cx = tgt_cx; sm_cy = tgt_cy; sm_init = true;
                    } else {
                        if (fabsf(tgt_cx - sm_cx) > px->cfg.jump_max) {
                            accept = false;
                            jump_skipped++;
                        } else {
                            sm_cx = px->cfg.smooth_alpha * tgt_cx + (1.f - px->cfg.smooth_alpha) * sm_cx;
                            sm_cy = px->cfg.smooth_alpha * tgt_cy + (1.f - px->cfg.smooth_alpha) * sm_cy;
                        }
                    }
                    tgt_cx = sm_cx;
                    tgt_cy = sm_cy;
                }

                bool in_dz = false;
                float np_ = cur_pan, nt_ = cur_tilt;
                const bool may_control = accept && fid >= settle_until_frame;
                if (may_control) {
                    in_dz = compute_control(px->cfg, tgt_cx, tgt_cy, cur_pan, cur_tilt,
                                            np_, nt_, tgt_frac_w);
                }

                locked = in_dz || !may_control;
                if (accept && !in_dz && fid >= settle_until_frame) {
                    cur_pan = np_;
                    cur_tilt = nt_;
                    px->gimbal->set_target(cur_pan, cur_tilt);
                    g_ctrl_sent++;
                    settle_until_frame = fid + (uint32_t)px->cfg.settle_frames;
                    // 原始版在这里画了个绿色十字标记目标点 —— 那是**显示**，
                    // 现在由 C 用同样的规则重画（目标点坐标随结果传过去）。
                }
            } else {
                if (++lost_frames > 30) { locked = false; sm_init = false; }
            }
        }
        auto t4 = std::chrono::steady_clock::now();
        g_st_ctrl.add(std::chrono::duration<double, std::milli>(t4 - t3).count());
        g_st_npu.add(npu_ms);
        g_st_decode.add(dec_ms);
        // 端到端延迟：A 抓到这一帧 → 现在控制算完（并已下发）
        g_st_latency.add(us_to_ms(now_us() - item.t_capture_us));

        // ---- 5. 打包给 C（只含显示需要的东西）----
        InferResult r;
        r.slot = s;
        r.dets_in_orig = std::move(dets_in_orig);
        r.t_capture_us = item.t_capture_us;
        r.frame_id = fid;
        r.cur_pan = cur_pan;
        r.cur_tilt = cur_tilt;
        r.locked = locked;
        r.has_target = has_target;
        r.rejected_partial = rejected_partial;
        r.npu_ms = npu_ms;
        r.dec_ms = dec_ms;

        // 状态词（原逻辑原样搬过来）
        if (!has_target && rejected_partial > 0) r.state_str = "NO_GOOD_TGT";
        else if (!has_target)                    r.state_str = "LOST";
        else if (fabsf(cur_pan) >= px->cfg.pan_range - 0.5f) r.state_str = "SATURATED";
        else if (locked)                         r.state_str = "LOCK";
        else                                     r.state_str = "TRACK";

        // 供 profile 行打印（原串行版这一行也打了这些，便于逐项对账）
        g_prof_pan = cur_pan;
        g_prof_tilt = cur_tilt;
        g_prof_rejected = rejected_partial;
        g_prof_state = (!has_target && rejected_partial > 0) ? 4
                     : (!has_target) ? 0
                     : (fabsf(cur_pan) >= px->cfg.pan_range - 0.5f) ? 3
                     : (locked) ? 2 : 1;

        // ★ 目标点也传给 C —— C 要画那个绿色十字。
        //   放这里是因为 C 没有 tgt_cx/cy（它在 B 的局部作用域里）。
        //   用两个 float 带走即可，不涉及所有权。
        r.tgt_cx = tgt_cx;
        r.tgt_cy = tgt_cy;
        r.tgt_frac_w = tgt_frac_w;

        px->q2->push(std::move(r), &g_q2_drop);
        g_b_frames++;
    }
    return nullptr;
}

// ---- Thread C：画框 + BEV + HUD + JPEG + 推流 ------------------------------
//
// ★ 为什么这些活可以放 C 而不用担心影响云台：
//   它们全是"给人看的"，晚一帧没有任何物理后果。C 慢只会让网页掉帧，
//   云台照样按 B 的节拍动。这正是三阶段拆分刻意造成的不对称：
//   **实时性要求高的（控制）放在靠前的阶段，可以妥协的（显示）放最后。**
//
// ★ 画框要用 B 重算一遍目标选择吗：
//   不用。B 已经把 dets_in_orig（原图坐标）和 tgt_cx/tgt_cy 传过来了，
//   C 只负责"画"。唯一需要 C 自己判的是"这个 person 框为什么没被追"
//   —— 那是纯几何规则（贴边/长宽比），用同一套常量复算即可，
//   保证画出来的是"真的被拒了"，而不是靠猜。
// ---- Thread C：画框 + BEV + HUD + JPEG + 推流 ------------------------------
//
// ★ 为什么这些活可以放 C 而不用担心影响云台：
//   它们全是"给人看的"，晚一帧没有任何物理后果。C 慢只会让网页掉帧，
//   云台照样按 B 的节拍动。这正是三阶段拆分刻意造成的不对称：
//   **实时性要求高的（控制）放在靠前的阶段，可以妥协的（显示）放最后。**
//
// ★ 为什么 C 里要**重画**目标选择而不直接复用 B 的结果：
//   能复用的一律复用（B 已经把 dets_in_orig 和 tgt_cx/cy 传过来了）。
//   C 自己只做两件 B 刻意没做的事，两件都是纯显示：
//     ① 给"检测到但没被追"的框画橙色框 + 标签（TOP_CUT / SIDE_CUT / ASPECT）
//        —— B 不画框，但拒绝原因必须让人看见，否则现场没法判断
//     ② 在目标点画绿色十字
//   拒绝原因是**纯几何规则**（贴边 + 长宽比），用同一组常量复算即可，
//   保证画出来的真的是"被拒了"，而不是靠猜。
//
// ★ JPEG 编码用的 jpg 容器每帧都会被 imencode 重新分配（约 100KB）。
//   这个**没有**优化成复用：imencode 的签名要求传一个空 vector 或按需 resize，
//   传已有内容的 vector 不保证复用；而且 100KB/帧 @30fps = 3MB/s，
//   相对 2.7MB/帧的图像完全不是重点。真要抠这里的分配，
//   应该换成 cv::imencode 到固定 cv::Mat 的路径，收益也就 1~2ms。
static void* thread_c_display(void* arg) {
    PipelineCtx* px = static_cast<PipelineCtx*>(arg);
    InferResult r;
    int64_t t_prev_c = 0;
    uint32_t prof_counter = 0;

    while (g_running) {
        if (!px->q2->pop(r)) break;
        MatSlot* s = r.slot;
        if (!s) continue;

        cv::Mat& bgr = s->bgr;
        auto tD0 = std::chrono::steady_clock::now();

        // ---- 画所有检测框 ----
        const float EDGE_MARGIN = 0.02f;
        const float ASPECT_MIN = 0.15f;
        const float ASPECT_MAX = 3.00f;
        const float HEAD_TOP_OFFSET = 0.10f;

        std::vector<std::pair<std::string, Box3D>> bev_objects;
        int obj_count = 0;

        // BEV 参数（与原版一致）
        const bool is_1080p = (bgr.cols >= 1920);
        const float hfov = is_1080p ? 72.0f : 65.0f;
        const float mount_h = is_1080p ? 1.50f : 1.60f;
        const float pitch = is_1080p ? 3.5f : 8.0f;
        const float range_fwd = is_1080p ? 60.0f : 50.0f;
        const float range_lat = 16.0f;
        const int radar_size = is_1080p ? 340 : 320;
        BEVProjector bev(bgr.cols, bgr.rows, hfov, mount_h, pitch);

        for (const auto& d : r.dets_in_orig) {
            const float ox1 = d.x1, oy1 = d.y1, ox2 = d.x2, oy2 = d.y2;

            // ---- ① "检测到但不追"的说明框 ----
            // 只有 person 才有"追不追"这个概念，其他类别照常画框。
            if (d.class_id == 0 && px->cfg.filter_partial) {
                const float bw = ox2 - ox1;
                const float bh = oy2 - oy1;
                const bool clip_left  = ox1 <= bgr.cols * EDGE_MARGIN;
                const bool clip_right = ox2 >= bgr.cols * (1.f - EDGE_MARGIN);
                const bool clip_top   = oy1 <= bgr.rows * EDGE_MARGIN;
                const float aspect = (bh > 1e-3f) ? (bw / bh) : 999.f;
                const bool bad_aspect = (aspect < ASPECT_MIN || aspect > ASPECT_MAX);
                const bool head_bad = clip_top || clip_left || clip_right;

                if (head_bad || bad_aspect) {
                    cv::rectangle(bgr, cv::Point((int)ox1, (int)oy1),
                                  cv::Point((int)ox2, (int)oy2),
                                  cv::Scalar(0, 140, 255), 1);
                    const char* why = clip_top ? "TOP_CUT"
                                    : (clip_left || clip_right) ? "SIDE_CUT"
                                    : "ASPECT";
                    cv::putText(bgr, why,
                                cv::Point((int)ox1 + 4, std::max(14, (int)oy1 - 5)),
                                cv::FONT_HERSHEY_SIMPLEX, 0.45,
                                cv::Scalar(0, 140, 255), 1, cv::LINE_AA);
                }
            }

            const bool is_traffic = is_traffic_class(d.class_id);
            if (!is_traffic && d.score < 0.40f) continue;

            const char* name = (d.class_id >= 0 && d.class_id < 80) ? COCO80[d.class_id] : "obj";
            const cv::Scalar col = PALETTE[obj_count % 6];
            obj_count++;

            cv::rectangle(bgr, cv::Point((int)ox1, (int)oy1),
                          cv::Point((int)ox2, (int)oy2), col, 2);

            Box3D box3d;
            const bool ground = bev.estimate_3d_box(ox1, oy1, ox2, oy2, name, box3d);
            if (ground && px->cfg.show_3d) {
                draw_3d_box(bgr, box3d, col, 2);
                bev_objects.push_back({name, box3d});
            }

            // 标签：类别 + 置信度 + 框尺寸
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

        // ---- ② 目标点绿色十字 ----
        // 原版画这个的条件是"这一帧真的下发了指令"。B 把决策结果带过来了，
        // 用 has_target && 非 locked 来近似"这一帧在动作"，语义与原版对齐
        // （原版那些变量都是每帧局部量，这里用 B 传回的对应量）。
        if (px->gimbal_on && r.has_target && !r.locked) {
            int bx = (int)(r.tgt_cx * bgr.cols);
            int by = (int)(r.tgt_cy * bgr.rows);
            cv::circle(bgr, cv::Point(bx, by), 10, cv::Scalar(0, 255, 0), 2, cv::LINE_AA);
            cv::line(bgr, cv::Point(bx - 16, by), cv::Point(bx + 16, by),
                     cv::Scalar(0, 255, 0), 1, cv::LINE_AA);
            cv::line(bgr, cv::Point(bx, by - 16), cv::Point(bx, by + 16),
                     cv::Scalar(0, 255, 0), 1, cv::LINE_AA);
        } else if (px->gimbal_on && r.locked) {
            cv::putText(bgr, "LOCKED", cv::Point(20, bgr.rows - 24),
                        cv::FONT_HERSHEY_SIMPLEX, 1.0, cv::Scalar(0, 255, 0), 2, cv::LINE_AA);
        }

        // 画面中心十字准星
        cv::line(bgr, cv::Point(bgr.cols / 2, 0), cv::Point(bgr.cols / 2, bgr.rows),
                 cv::Scalar(60, 160, 60), 1, cv::LINE_AA);
        cv::line(bgr, cv::Point(0, bgr.rows / 2), cv::Point(bgr.cols, bgr.rows / 2),
                 cv::Scalar(60, 160, 60), 1, cv::LINE_AA);

        // BEV 雷达
        cv::Mat radar = render_bev_radar(radar_size, range_fwd, range_lat, bev_objects);
        int margin = 16;
        int rx = bgr.cols - radar.cols - margin;
        int ry = margin + 36;
        if (rx >= 0 && ry + radar.rows <= bgr.rows) {
            radar.copyTo(bgr(cv::Rect(rx, ry, radar.cols, radar.rows)));
            cv::rectangle(bgr, cv::Rect(rx - 1, ry - 1, radar.cols + 2, radar.rows + 2),
                          cv::Scalar(0, 200, 255), 1);
        }

        // HUD
        // ★ fps 用**实测的出图帧率**（C 自己的节拍），不是各段相加算出来的。
        //   相加算出来的数在流水线里没有意义（各段是重叠的）。
        //
        // ★ 这里为什么用 0.5 秒的滑动窗、且初值是 0：
        //   原来我写成 `double fps_meas = g_c_frames.load();`，而 g_c_frames 是
        //   **累计帧数**（不是帧率）—— 启动后第一秒还没攒满一个窗口时，
        //   它就把累计值（259）当成 fps 显示出来，/status 里也就跟着报 259。
        //   现在初值 0，窗口没攒满就沿用上一次算出的值（g_last_fps）。
        const int64_t t_now_c = now_us();
        static int64_t t_fps_ref = 0;
        static uint64_t fps_ref_frames = 0;
        static double g_last_fps = 0.0;
        if (t_fps_ref == 0) { t_fps_ref = t_now_c; fps_ref_frames = g_c_frames.load(); }
        double dt_fps = us_to_ms(t_now_c - t_fps_ref) / 1000.0;
        if (dt_fps >= 0.5) {
            g_last_fps = (double)(g_c_frames.load() - fps_ref_frames) / dt_fps;
            t_fps_ref = t_now_c;
            fps_ref_frames = g_c_frames.load();
        }
        const double fps_meas = g_last_fps;

        char hud[384];
        if (px->gimbal_on) {
            snprintf(hud, sizeof(hud),
                     "EdgeVision RK3588 | NPU %.1f ms + Dec %.1f ms | %.1f FPS | "
                     "Gimbal P%+.1f T%+.1f | %s | objs %zu",
                     r.npu_ms, r.dec_ms, fps_meas, r.cur_pan, r.cur_tilt,
                     r.state_str, bev_objects.size());
        } else {
            snprintf(hud, sizeof(hud),
                     "EdgeVision RK3588 | NPU %.1f ms + Dec %.1f ms | %.1f FPS | "
                     "(no gimbal) | objs %zu",
                     r.npu_ms, r.dec_ms, fps_meas, bev_objects.size());
        }
        cv::rectangle(bgr, cv::Point(0, 0), cv::Point(bgr.cols, 36), cv::Scalar(15, 23, 42), -1);
        cv::putText(bgr, hud, cv::Point(16, 26), cv::FONT_HERSHEY_SIMPLEX, 0.62,
                    cv::Scalar(56, 189, 248), 2, cv::LINE_AA);

        auto tD1 = std::chrono::steady_clock::now();

        // ---- 更新 /status ----
        // ★ 语义与原版一致：pan/tilt 用 B 算出的值（云台实际的目标角度），
        //   sent/errors 从 GimbalSerial 读（它内部有锁）。
        {
            float gp, gt; int ge, gs;
            if (px->gimbal_on) px->gimbal->state(gp, gt, ge, gs);
            else { gp = r.cur_pan; gt = r.cur_tilt; ge = 0; gs = 0; }
            pthread_mutex_lock(&g_status.lock);
            g_status.pan = px->gimbal_on ? gp : r.cur_pan;
            g_status.tilt = px->gimbal_on ? gt : r.cur_tilt;
            g_status.locked = r.locked;
            g_status.sent = gs;
            g_status.errors = ge;
            g_status.objects = (int)bev_objects.size();
            g_status.npu_ms = r.npu_ms;
            g_status.fps = fps_meas;
            g_status.dropped = g_cam_dropped;
            g_status.reopens = g_cam_reopens;
            pthread_mutex_unlock(&g_status.lock);
        }

        // ---- JPEG 编码 ----
        auto tE0 = std::chrono::steady_clock::now();
        std::vector<uint8_t> jpg;
        bool jpg_ok = false;
        try {
            jpg_ok = cv::imencode(".jpg", bgr, jpg, {cv::IMWRITE_JPEG_QUALITY, 80});
        } catch (const cv::Exception& e) {
            std::cerr << "[!] JPEG 编码异常（已计数，沿用上一张）: " << e.what() << "\n";
        }
        if (!jpg_ok || jpg.empty()) g_cam_badjpeg++;
        auto tE1 = std::chrono::steady_clock::now();

        g_st_draw.add(std::chrono::duration<double, std::milli>(tD1 - tD0).count());
        g_st_jpeg.add(std::chrono::duration<double, std::milli>(tE1 - tE0).count());
        if (t_prev_c) g_st_periodC.add(us_to_ms(t_now_c - t_prev_c));
        t_prev_c = t_now_c;

        // ★ 只有真的编出图才发布（原逻辑：编码失败不能把上一张好图清掉，
        //   否则 /stream 的 `if (empty) continue` 会把客户端挂住）
        if (jpg_ok && !jpg.empty()) {
            pthread_mutex_lock(&g_jpeg_lock);
            g_latest_jpeg = std::move(jpg);
            pthread_cond_broadcast(&g_jpeg_cond);
            pthread_mutex_unlock(&g_jpeg_lock);
        }

        // ---- 发布到 9000 ----
        // ★ 这一步放在 C 而不是 B：publish_detections 会拿 g_det_lock
        //   并 broadcast 给所有 9000 客户端，属于"对外推送"，慢一点没关系，
        //   不能让 B 等它（B 是控制的关键路径）。
        publish_detections(r.frame_id, r.dets_in_orig, bgr.cols, bgr.rows);

        // ---- 还 Slot 回池 ----
        // ★ 这一步必须放在最后：bgr 已经编码完了，C 不再需要它。
        px->pool->release(s);
        s = nullptr;

        g_c_frames++;
        if (++prof_counter % 60 == 0) print_pipeline_profile();
    }
    return nullptr;
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
    // 流水线 Slot 池大小（见 --pipe-slots 注释）
    int pipe_slots = 3;
    const char* bridge_host = nullptr;
    int bridge_port = 9100;
    // 用户是否显式指定过 pan 方向。为 true 时，默认值推导（按 --rotate）不覆盖它。
    bool invert_pan_explicit = false;
    // 过滤开关是否被显式指定（见下面 --target-filter / --no-target-filter 的处理）
    bool filter_explicit = false;
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
        // --max-step-tilt：tilt 轴独立的单帧步长上限（2026-11-16 tilt 振荡修复新增）。
        //   为什么必须分轴：pan 1 指令单位 = 2° 物理角（42.7px），
        //   tilt 1 指令单位 = 4° 物理角（80.0px，固件 TILT_RANGE_DEG=45 对应 180° 全行程），
        //   tilt 是 pan 的 1.87 倍 ⇒ 同样的 max_step，tilt 的单帧画面位移是 pan 的 1.87 倍。
        //   共用 0.6 时 tilt 单帧 48px > 死区 32px ⇒ 每帧越过死区 ⇒ 极限环（"上下摆好几轮"）。
        //   推导见 ControlCfg 里 max_step 上方那段，**别再把两轴合并回一个参数**。
        else if (strcmp(argv[i], "--max-step-tilt") == 0 && i + 1 < argc) cfg.max_step_tilt = atof(argv[++i]);
        // --slow-gain：变步长的临界比例。0 = 刚出死区完全不动（会黏滞），
        //   1 = 退化成固定步长（会和以前一样振）。0.35 是 Python 版实测值。
        else if (strcmp(argv[i], "--slow-gain") == 0 && i + 1 < argc) cfg.slow_gain = atof(argv[++i]);
        // tilt 安全区间：默认 [-35, -5]。换安装高度/俯仰后可能要放宽。
        else if (strcmp(argv[i], "--tilt-safe-min") == 0 && i + 1 < argc) cfg.tilt_safe_min = atof(argv[++i]);
        else if (strcmp(argv[i], "--tilt-safe-max") == 0 && i + 1 < argc) cfg.tilt_safe_max = atof(argv[++i]);
        else if (strcmp(argv[i], "--deadzone") == 0 && i + 1 < argc) cfg.deadzone = atof(argv[++i]);
        // --deadzone-tilt：tilt 轴独立死区（2026-11-16 tilt 振荡修复新增）。
        //   不传就用默认 0.035（0.035×720 = 25.2px）。为什么 tilt 要比 pan(0.025) 大：
        //   见 ControlCfg 里 deadzone_tilt 上方那段 —— 归一化只统一了"像素→比例"，
        //   没统一"比例→舵机指令"，同样 0.025 换到 tilt 只有 0.225 指令单位（pan 的 0.75 的 1/3），
        //   tilt 反而更容易被判出死区。同时蹲起是低频大幅动作，不需要追太细。
        else if (strcmp(argv[i], "--deadzone-tilt") == 0 && i + 1 < argc) cfg.deadzone_tilt = atof(argv[++i]);
        // --start-pan / --start-tilt：启动时先摆到这个姿态（2026-11-17）
        //   解决"中位看不到人 → 往错误方向追到限位"的问题。
        else if (strcmp(argv[i], "--start-pan")  == 0 && i + 1 < argc) cfg.start_pan  = atof(argv[++i]);
        else if (strcmp(argv[i], "--start-tilt") == 0 && i + 1 < argc) cfg.start_tilt = atof(argv[++i]);
        else if (strcmp(argv[i], "--min-score") == 0 && i + 1 < argc) cfg.min_score = atof(argv[++i]);
        else if (strcmp(argv[i], "--no-invert-pan") == 0) { cfg.invert_pan = false; invert_pan_explicit = true; }
        else if (strcmp(argv[i], "--invert-pan") == 0) { cfg.invert_pan = true; invert_pan_explicit = true; }
        else if (strcmp(argv[i], "--no-invert-tilt") == 0) cfg.invert_tilt = false;
        else if (strcmp(argv[i], "--invert-tilt") == 0) cfg.invert_tilt = true;
        else if (strcmp(argv[i], "--no-pan") == 0) cfg.enable_pan = false;
        else if (strcmp(argv[i], "--no-tilt") == 0) cfg.enable_tilt = false;
        else if (strcmp(argv[i], "--smooth") == 0 && i + 1 < argc) cfg.smooth_alpha = atof(argv[++i]);
        else if (strcmp(argv[i], "--jump-max") == 0 && i + 1 < argc) cfg.jump_max = atof(argv[++i]);
        // 质量筛选开关（2026-11-16 新增）：
        //   原来只有 --no-target-filter 一条，而它只是把默认值 false 再设一次 false，
        //   结果是**全程序没有任何办法把过滤打开**（默认关 + 只能关 = 永远是关）。
        //   本次改动把判据修对了（底边贴边放行 + 追头部），过滤才值得默认打开，
        //   所以这里补上 --target-filter，并在 loop 里按 g_rotate 决定默认值。
        else if (strcmp(argv[i], "--no-target-filter") == 0) { cfg.filter_partial = false; filter_explicit = true; }
        else if (strcmp(argv[i], "--target-filter") == 0) { cfg.filter_partial = true; filter_explicit = true; }
        else if (strcmp(argv[i], "--settle") == 0 && i + 1 < argc) cfg.settle_frames = atoi(argv[++i]);
        else if (strcmp(argv[i], "--no-3d") == 0) cfg.show_3d = false;
        // --pipe-slots N：流水线 Slot 池大小（默认 3）。
        //   每块约 5.1MB（bgr 2.7 + lb 1.2 + rgb 1.2）。3 是"每阶段各持一帧"的
        //   最小可行值；调大只会多占内存，不会提高帧率（瓶颈在 B 的 NPU 17ms）。
        //   调成 2 会立刻看到 Q1 丢帧暴涨 —— 那是验证"背压真的生效"的好办法。
        else if (strcmp(argv[i], "--pipe-slots") == 0 && i + 1 < argc) pipe_slots = atoi(argv[++i]);
        // --rotate N：摄像头朝向矫正，N ∈ {0, 90, 180, 270}（逆时针）
        // 摄像头倒装用 180；侧装用 90/270。见主循环里的实现注释。
        else if (strcmp(argv[i], "--rotate") == 0 && i + 1 < argc) {
            int r = atoi(argv[++i]);
            if (r == 0 || r == 90 || r == 180 || r == 270) {
                g_rotate = r;
            } else {
                // 宁可报错退出，也不要静默忽略：静默忽略会让人以为
                // "我加了 --rotate 但没生效"，去查摄像头是不是反的。
                std::cerr << "[-] --rotate 只接受 0/90/180/270，收到: " << r << "\n";
                return 1;
            }
        }
    }

    // ---- 质量筛选默认打开（2026-11-16）--------------------------------
    //
    // 这条默认值必须在**参数循环之后**设，因为它和 --target-filter /
    // --no-target-filter 是同一个变量：放在 ControlCfg 里当初值会被命令行
    // 覆盖没问题，但真正的坑是「默认 false + 只有 --no-target-filter 一个开关」
    // = 永远打不开。这里统一在循环后决定默认值，并尊重用户显式指定。
    //
    // 判据已经修对了（顶边贴边才拒、底边放行、追头部位置），
    // 打开它才能把"框到了但不追"的坏目标挡掉，同时不再误杀近距离目标。
    if (!filter_explicit) cfg.filter_partial = true;

    // ---- invert_pan 按 --rotate 自动推导（2026-11-16）------------------
    //
    // 为什么必须自动推导、不能写死：invert_pan 的本质是「画面坐标系的 x 方向」，
    // 而 --rotate 会整幅重排坐标系，两者是同一件事的两面。
    //
    // 推导链（每一步都可查证）：
    //   ① 控制律（compute_control）：d_pan = +dx*fov*kp，再 if(invert_pan) 取反。
    //      ⇒ invert_pan=true  的净效果 = 目标偏右(dx>0) → pan 减小
    //      ⇒ invert_pan=false 的净效果 = 目标偏右(dx>0) → pan 增大
    //   ② 2026-11-03 三位置标定（pan=−15/0/+15 → cx=746.9/878.6/947.2）
    //      是在**未旋转**的画面上测的：pan 增大 → cx 增大 ⇒ 正反馈 ⇒ 需要 true。
    //   ③ 2026-11-16 Python 侧实测（head_tracking.py 直接读原始帧、无 flip）：
    //      目标偏右 → pan 必须减小 ⇒ 同样回到「原始帧 invert_pan=true」，与②一致。
    //   ④ 但本程序用 --rotate 180 启动，主循环做 cv::flip(bgr,rot,-1)：
    //          cx_rotated = W - cx_raw
    //      「旋转后偏右」== 「原始帧偏左」，x 方向整体镜像 ⇒ 符号翻一次
    //      ⇒ 旋转 180° 时应为 **false**。
    //   所以：raw 坐标系 = true，180° 坐标系 = false。
    //   90/270 会把 pan 与 tilt 的定义互换（那是另一套标定问题），
    //   本程序从未在 90/270 下标定过，这里只对 0/180 给出确定值，
    //   90/270 保持 true 并打印警告让人实测。
    //
    // 命令行显式给 --invert-pan / --no-invert-pan 时以命令行为准：
    //   这里用 invert_pan_explicit 标记，避免默认推导覆盖用户意图。
    if (!invert_pan_explicit) {
        if (g_rotate == 180) {
            cfg.invert_pan = false;
        } else if (g_rotate == 0) {
            cfg.invert_pan = true;
        } else {
            cfg.invert_pan = true;
            std::cerr << "[!] --rotate " << g_rotate
                      << " 下 pan 轴符号未标定（90/270 会把 pan/tilt 定义互换），"
                         "invert_pan 暂用 true，请实测确认\n";
        }
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
            // ★ 横幅必须把**分轴**参数都打出来（2026-11-16 tilt 振荡修复）：
            //   以前只打一个 max_step，两个轴共用，看不出 tilt 是不是被单独限住了。
            //   现在 tilt 有独立参数，现场调参时"看到的值 = 生效的值"才不会误判
            //   （踩过坑：以为 --max-step-tilt 没生效，其实是横幅没打印）。
            printf("[+] 云台控制已启用（TCP 桥接）  kp=(%.2f,%.2f) "
                   "max_step=(pan=%.2f,tilt=%.2f) deadzone=(pan=%.3f,tilt=%.3f) "
                   "slow_gain=%.2f "
                   "invert=(pan=%d,tilt=%d) enable=(pan=%d,tilt=%d) min_score=%.2f "
                   "tilt_safe=[%.0f,%.0f] filter=%d rotate=%d\n",
                   cfg.kp_pan, cfg.kp_tilt, cfg.max_step, cfg.max_step_tilt,
                   cfg.deadzone, cfg.deadzone_tilt, cfg.slow_gain,
                   (int)cfg.invert_pan, (int)cfg.invert_tilt,
                   (int)cfg.enable_pan, (int)cfg.enable_tilt, cfg.min_score,
                   cfg.tilt_safe_min, cfg.tilt_safe_max,
                   (int)cfg.filter_partial, g_rotate);
            // 分轴参数的单位换算摊开印一行：现场一旦怀疑"tilt 为什么比 pan 慢"，
            // 这里就是答案（tilt 1 单位 = 80px，pan 1 单位 = 42.7px），省得再翻源码。
            printf("[i] 单帧位移上限: pan %.2f×42.7=%.1fpx | tilt %.2f×80.0=%.1fpx "
                   "（tilt 独立限幅，见 --max-step-tilt 注释）\n",
                   cfg.max_step, cfg.max_step * 42.7f,
                   cfg.max_step_tilt, cfg.max_step_tilt * 80.0f);

            // ── 启动姿态（2026-11-17）──────────────────────────────
            //   如果指定了 --start-pan/--start-tilt，先摆过去再开始追。
            //   为什么需要：舵机固定后中位可能看不到人，不摆过去就会往错误方向
            //   一直追、顶到限位卡死（用户实际遇到：P-90 卡住动不了）。
            if (!isnan(cfg.start_pan) || !isnan(cfg.start_tilt)) {
                float sp = isnan(cfg.start_pan)  ? 0.0f : cfg.start_pan;
                float st = isnan(cfg.start_tilt) ? 0.0f : cfg.start_tilt;
                char cmd[64];
                int n = snprintf(cmd, sizeof(cmd), "P %.2f %.2f\n", sp, st);
                if (write(gimbal.fd_for_start(), cmd, n) > 0) {
                    printf("[+] 启动姿态: P %+.1f %+.1f（等待到位）\n", sp, st);
                    usleep(1500000);      // 等舵机转过去（1.5s 够走满行程）
                }
            }
        } else {
            std::cerr << "[!] 桥接服务连不上，退化为纯预览模式\n";
            std::cerr << "    先跑：/root/rknn_env/bin/python /root/esp32_gimbal/gimbal_bridge.py\n";
        }
    } else if (gimbal_dev) {
        if (gimbal.open(gimbal_dev)) {
            gimbal_on = true;
            printf("[+] 云台控制已启用  kp=(%.2f,%.2f) "
                   "max_step=(pan=%.2f,tilt=%.2f) deadzone=(pan=%.3f,tilt=%.3f) "
                   "slow_gain=%.2f "
                   "invert=(pan=%d,tilt=%d) enable=(pan=%d,tilt=%d) min_score=%.2f "
                   "tilt_safe=[%.0f,%.0f] filter=%d rotate=%d\n",
                   cfg.kp_pan, cfg.kp_tilt, cfg.max_step, cfg.max_step_tilt,
                   cfg.deadzone, cfg.deadzone_tilt, cfg.slow_gain,
                   (int)cfg.invert_pan, (int)cfg.invert_tilt,
                   (int)cfg.enable_pan, (int)cfg.enable_tilt, cfg.min_score,
                   cfg.tilt_safe_min, cfg.tilt_safe_max,
                   (int)cfg.filter_partial, g_rotate);
            printf("[i] 单帧位移上限: pan %.2f×42.7=%.1fpx | tilt %.2f×80.0=%.1fpx "
                   "（tilt 独立限幅，见 --max-step-tilt 注释）\n",
                   cfg.max_step, cfg.max_step * 42.7f,
                   cfg.max_step_tilt, cfg.max_step_tilt * 80.0f);
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

    // ========================================================================
    //  启动三阶段流水线
    // ========================================================================
    //
    // 调参入口：--pipe-slots N 可以改池大小（默认 3）。
    //   3 是"每阶段各持一帧"的最小可行值；调大只会多占内存（每块 ~5MB），
    //   不会提高帧率 —— 因为瓶颈在 B 的 17ms，不在池深度。
    //   调成 2 会立刻表现为 q1 丢帧暴涨（A 拿不到块）。
    SlotPool pool((size_t)pipe_slots);

    FrameQ  q1;
    InferQ  q2;

    PipelineCtx px;
    px.cap = &cap;
    px.dev_path = dev_path;
    px.is_video_file = is_video_file;
    px.ctx = ctx;
    px.outputs = &outputs;
    px.bi = bi; px.ci = ci;
    px.bnhwc = bnhwc; px.cnhwc = cnhwc;
    px.cfg = cfg;
    px.gimbal_on = gimbal_on;
    px.gimbal = &gimbal;
    px.q1 = &q1;
    px.q2 = &q2;
    px.pool = &pool;

    printf("[+] 流水线启动：3 线程 / 队列容量 1（丢旧）/ Slot 池 %d 块"
           "（约 %.1f MB 常驻）\n",
           pipe_slots, pipe_slots * 5.1);

    pthread_t tid_a, tid_b, tid_c;
    pthread_create(&tid_a, nullptr, thread_a_capture, &px);
    pthread_create(&tid_b, nullptr, thread_b_infer,   &px);
    pthread_create(&tid_c, nullptr, thread_c_display, &px);

    // ---- 主线程只负责"等退出信号"----
    //
    // ★ 为什么 main 不参与干活：
    //   主线程如果也进流水线，它就得同时处理退出逻辑，退出路径会复杂得多。
    //   这里让它只做一个 100ms 醒来一次的看门狗，逻辑简单、退出可靠。
    while (g_running) usleep(100000);

    // ========================================================================
    //  优雅退出
    // ========================================================================
    //
    // ★ 顺序很重要，写错会挂死：
    //   ① 先 close 队列 —— 唤醒所有卡在 pop() 里等的线程
    //   ② 再 close 池   —— 唤醒所有卡在 acquire() 里等的线程
    //   ③ 最后 join
    //
    // ★ 为什么"只设 g_running=0"不够（这是最容易踩的坑）：
    //   g_running 是 volatile sig_atomic_t，它**不会**触发任何条件变量通知。
    //   如果某个线程正好在 `cv_.wait(lk, pred)` 里睡着，而 pred 里没有
    //   g_running（或者信号恰好落在"判完 pred、还没真正睡下去"那个窗口），
    //   它就永远醒不过来 —— 经典的 lost-wakeup。
    //   所以必须由 close() 显式 notify_all，这是**唯一**可靠的唤醒方式。
    //
    // ★ 关于"程序对 SIGTERM 杀不死"的既有问题（HTTP 线程卡在 accept）：
    //   本次**没有**去改它（超出任务范围）。但新增的三个线程不受它影响：
    //   它们全部靠 close() 唤醒，join 会在毫秒级返回。
    //   HTTP 线程仍然会卡在 accept()，所以进程仍需 SIGKILL 收尾 ——
    //   这是改动前就存在的行为，不是本次引入的。
    q1.close();
    q2.close();
    pool.close();
    pthread_cond_broadcast(&g_jpeg_cond);    // 唤醒 /stream 里等待的 HTTP 线程
    pthread_cond_broadcast(&g_det_cond);     // 唤醒 9000 里等待的线程

    pthread_join(tid_a, nullptr);
    pthread_join(tid_b, nullptr);
    pthread_join(tid_c, nullptr);

    // 收尾统计：把退出前最后一段的 profile 打出来，便于对照
    printf("[+] 流水线已停止：A %llu 帧 / B %llu 帧 / C %llu 帧"
           "（Q1 丢 %llu / Q2 丢 %llu）\n",
           (unsigned long long)g_a_frames.load(), (unsigned long long)g_b_frames.load(),
           (unsigned long long)g_c_frames.load(),
           (unsigned long long)g_q1_drop.load(), (unsigned long long)g_q2_drop.load());
    fflush(stdout);

    cap.release();
    rknn_destroy(ctx);
    pthread_join(http_tid, nullptr);
    return 0;
}
