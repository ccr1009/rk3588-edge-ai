// ============================================================================
//  test_cam_read_harness.cpp —— 抗 MJPEG 断帧的离线验收（不占摄像头、不需要板子）
//
//  为什么要有这个文件
//  ------------------
//  w4_edgevision_gimbal_preview.cpp 的 cam_read()/open_camera() 是为了修
//  「cap >> bgr 抛 cv::Exception → Aborted」（TROUBLESHOOTING 第 53 条）。
//  但那个崩溃是**偶发**的：驱动什么时候给零长度 buffer，你控制不了，
//  等它自然复现可能要跑很久。所以这里用**编程注入坏帧**的方式把行为钉死。
//
//  做法：把源码里那两段函数**原文**抽进本文件（标了 BEGIN/END_UNDER_TEST），
//  配一个最小的 `cv::VideoCapture` stub —— 它能按帧号决定「抛异常 / 返回 false」，
//  于是"第几帧坏"完全可控。
//
//  编译与运行（任意有 g++ 的机器，包括板子）：
//      g++ -O2 -std=c++17 -Wall -Wextra -o test_cam_read_harness test_cam_read_harness.cpp
//      ./test_cam_read_harness
//
//  ★ 维护约定：改了 w4_edgevision_gimbal_preview.cpp 里的 cam_read()/open_camera()，
//    必须同步这两个函数到下面 BEGIN/END_UNDER_TEST 之间，否则本 harness
//    测的是旧逻辑（第 48 条：改了一份不生效的那份还在 —— 别在本文件上重演）。
//
//  断言覆盖：
//    ① 断帧被吞住（异常不逃逸到调用方）
//    ② 断帧被准确计数
//    ③ 坏帧后 dst 被清空（不会拿旧帧冒充新帧）
//    ④ 连续 8 帧失败触发一次重开
//    ⑤ 偶发断帧（每 60 帧 1 次）不误触发重开
//    ⑥ 预热阶段断帧也不崩
// ============================================================================
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <unistd.h>
#include <iostream>

// ---- 最小 cv stub：只覆盖被抽出的那两段代码用到的接口 ----
namespace cv {
struct Exception {
    const char* msg_;
    explicit Exception(const char* m) : msg_(m) {}
    const char* what() const { return msg_; }
};
class Mat {
public:
    bool empty() const { return empty_; }
    void release() { empty_ = true; }
    int id_ = 0;          // 用于证伪"旧帧残留"
    bool empty_ = true;
};
enum { CAP_V4L2 = 200, CAP_PROP_FOURCC = 6, CAP_PROP_FRAME_WIDTH = 3,
       CAP_PROP_FRAME_HEIGHT = 4, CAP_PROP_FPS = 5, CAP_PROP_BUFFERSIZE = 38,
       CAP_PROP_POS_FRAMES = 1 };
class VideoWriter {
public:
    static int fourcc(char, char, char, char) { return 0x47504a4d; }
};
class VideoCapture {
public:
    // 可编程行为：throw_at_ / fail_at_ 里列出的帧号会抛 / 返回 false
    int frame_ = 0;
    int throw_at_[64] = {0}; int nth_ = 0;
    int fail_at_[64]  = {0}; int nf_ = 0;
    bool opened_ = false;
    int opens_ = 0;
    void release() { opened_ = false; }
    bool isOpened() const { return opened_; }
    bool open(const char*) { opened_ = true; opens_++; return true; }
    bool open(const char*, int) { opened_ = true; opens_++; return true; }
    bool set(int, double) { return true; }
    bool should_throw() const { for (int i = 0; i < nth_; ++i) if (throw_at_[i] == frame_) return true; return false; }
    bool should_fail()  const { for (int i = 0; i < nf_;  ++i) if (fail_at_[i]  == frame_) return true; return false; }
    bool read(Mat& m) {
        ++frame_;
        if (should_throw()) throw Exception("stub: !buf.empty() in function 'imdecode_'");
        if (should_fail()) return false;   // 关键：返回 false 且不改 m
        m.empty_ = false; m.id_ = frame_;
        return true;
    }
};
// OpenCV 的 `cap >> mat` 是 (VideoCapture&, Mat&) 重载；stub 必须提供，
// 否则测不到 open_camera() 里那段预热代码。
inline VideoCapture& operator>>(VideoCapture& cap, Mat& m) { cap.read(m); return cap; }
} // namespace cv

// ======== 以下为被测源码（来自 w4_edgevision_gimbal_preview.cpp）========
//__BEGIN_UNDER_TEST__
static uint64_t g_cam_dropped     = 0;
static uint64_t g_cam_reopens     = 0;
static int      g_cam_consec_fail = 0;
static const int CAM_REOPEN_AFTER = 8;

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
    try { cap.set(cv::CAP_PROP_BUFFERSIZE, 1); } catch (const cv::Exception&) {}

    cv::Mat warm;
    for (int i = 0; i < 15; ++i) {
        try {
            cap >> warm;
        } catch (const cv::Exception& e) {
            g_cam_dropped++;
            std::cerr << "[!] 预热阶段断帧（已计数）: " << e.what() << "\n";
        }
    }
    if (verbose) printf("[+] 已打开摄像头: %s @ 1280x720 30FPS\n", dev_path);
    return true;
}

static bool cam_read(cv::VideoCapture& cap, cv::Mat& dst) {
    dst.release();

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
//__END_UNDER_TEST__

// ============================ 测试 ============================
static int g_fail = 0;
#define CHECK(cond, fmt, ...) do { \
    if (cond) { printf("  [PASS] " fmt "\n", ##__VA_ARGS__); } \
    else { printf("  [FAIL] " fmt "\n", ##__VA_ARGS__); g_fail++; } } while (0)

int main() {
    // --- T1: 单帧断帧不再逃出循环 ---
    printf("T1 断帧被吞住并计数\n");
    {
        g_cam_dropped = g_cam_reopens = 0; g_cam_consec_fail = 0;
        cv::VideoCapture cap; cv::Mat dst;
        cap.throw_at_[0] = 5; cap.nth_ = 1;
        cap.open("/dev/video81", cv::CAP_V4L2);
        int ok_cnt = 0;
        for (int i = 0; i < 10; ++i) {
            bool got_exc = false;
            try { if (cam_read(cap, dst)) ok_cnt++; }
            catch (...) { got_exc = true; }
            CHECK(!got_exc, "第 %d 帧没有异常逃逸", i + 1);
            if (got_exc) break;
        }
        CHECK(g_cam_dropped == 1, "dropped==1 (实际 %llu)", (unsigned long long)g_cam_dropped);
        CHECK(ok_cnt == 9, "成功 9 帧 (实际 %d)", ok_cnt);
    }

    // --- T2: 失败时 dst 必须被清空（否则旧帧冒充新帧）---
    printf("T2 失败帧不复用旧图\n");
    {
        g_cam_dropped = 0; g_cam_consec_fail = 0;
        cv::VideoCapture cap; cv::Mat dst;
        cap.open("/dev/video81", cv::CAP_V4L2);
        CHECK(cam_read(cap, dst) && !dst.empty(), "第 1 帧有效");
        int first_id = dst.id_;
        cap.fail_at_[0] = 2; cap.nf_ = 1;   // 下一帧 read() 返回 false
        bool ok = cam_read(cap, dst);
        CHECK(!ok, "第 2 帧被判为坏帧");
        CHECK(dst.empty(), "坏帧后 dst 已清空（不会残留 id=%d）", first_id);
    }

    // --- T3: 连续 8 帧 → 调用方应重开设备 ---
    printf("T3 连续失败达阈值触发重开\n");
    {
        g_cam_dropped = 0; g_cam_reopens = 0; g_cam_consec_fail = 0;
        cv::VideoCapture cap; cv::Mat dst;
        cap.open("/dev/video81", cv::CAP_V4L2);
        for (int i = 0; i < 8; ++i) cap.throw_at_[i] = i + 1;
        cap.nth_ = 8;
        // 复刻主循环里的处理逻辑
        for (int i = 0; i < 8; ++i) {
            if (!cam_read(cap, dst)) {
                if (g_cam_consec_fail >= CAM_REOPEN_AFTER) {
                    g_cam_reopens++;
                    g_cam_consec_fail = 0;
                    open_camera(cap, "/dev/video81", false, false);
                }
            }
        }
        CHECK(g_cam_reopens == 1, "重开 1 次 (实际 %llu)", (unsigned long long)g_cam_reopens);
        // 预热 15 帧落在帧号 9..23，stub 只让 1..8 抛，所以计数应正好 8。
        // （顺带证明：重开后的预热确实又走了 15 帧，没有把旧状态的计数搅乱）
        CHECK(g_cam_dropped == 8, "断帧计数=8 (实际 %llu)", (unsigned long long)g_cam_dropped);
        CHECK(cap.frame_ == 23, "重开后确实又预热了 15 帧（帧号到 23，实际 %d）", cap.frame_);
    }

    // --- T4: 偶发断帧（每 60 帧一次，持续 600 帧）不应触发重开 ---
    printf("T4 偶发断帧不误触发重开\n");
    {
        g_cam_dropped = g_cam_reopens = 0; g_cam_consec_fail = 0;
        cv::VideoCapture cap; cv::Mat dst;
        cap.open("/dev/video81", cv::CAP_V4L2);
        int n = 0;
        for (int f = 60; f <= 600; f += 60) cap.throw_at_[n++] = f;
        cap.nth_ = n;
        int ok_cnt = 0;
        for (int i = 0; i < 600; ++i) {
            if (!cam_read(cap, dst)) {
                if (g_cam_consec_fail >= CAM_REOPEN_AFTER) {
                    g_cam_reopens++; g_cam_consec_fail = 0;
                    open_camera(cap, "/dev/video81", false, false);
                }
                continue;
            }
            ok_cnt++;
        }
        CHECK(g_cam_reopens == 0, "0 次重开 (实际 %llu)", (unsigned long long)g_cam_reopens);
        CHECK(ok_cnt == 590, "成功 590 帧 (实际 %d)", ok_cnt);
        // 预热 15 帧成功（stub 只按帧号抛，帧号 1..15 没配异常）
        CHECK(g_cam_dropped == 10, "断帧 10 次 (实际 %llu)", (unsigned long long)g_cam_dropped);
    }

    // --- T5: 预热阶段断帧也要被吞住 ---
    printf("T5 预热阶段断帧不崩\n");
    {
        g_cam_dropped = 0; g_cam_reopens = 0; g_cam_consec_fail = 0;
        cv::VideoCapture cap;
        cap.throw_at_[0] = 3; cap.nth_ = 1;   // 预热第 3 帧抛
        bool escaped = false;
        try { open_camera(cap, "/dev/video81", false); }
        catch (...) { escaped = true; }
        CHECK(!escaped, "open_camera() 没有异常逃逸");
        CHECK(g_cam_dropped == 1, "预热断帧计到 1 (实际 %llu)", (unsigned long long)g_cam_dropped);
    }

    printf("\n==== %s (fail=%d) ====\n", g_fail ? "有失败" : "全部通过", g_fail);
    return g_fail ? 1 : 0;
}
