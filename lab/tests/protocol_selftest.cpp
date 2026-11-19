// protocol_selftest.cpp —— 248B 帧协议的**可在 PC 上跑的**验收测试
//
// 为什么需要它：
//   头文件里的 static_assert 只在"有人编译它"时才生效。而项目主线源码
//   （rkllm_embodied_agent / edgevision_gateway ...）都依赖 RKNN、OpenCV、
//   RKLLM runtime，**只能在板子上编**（跨架构 + glibc 版本约束）。
//   如果只靠它们来验证协议，那"协议没漂"这件事在离开板子时就无法证明。
//
//   这个测试只用标准 C++，PC 和板子都能编、都能跑，so 它可以在任何一次
//   改动之后立刻给出答案：契约还在不在。
//
// 用法：
//   g++ -std=c++17 -Ilab/include lab/tests/protocol_selftest.cpp -o /tmp/pst
//   /tmp/pst
//
// 退出码：0 = 全部通过；1 = 有检查失败

#include "edgevision_protocol.h"

#include <cstdio>
#include <cstring>

static int g_failed = 0;

static void check(bool ok, const char* what) {
    if (ok) {
        std::printf("  [PASS] %s\n", what);
    } else {
        std::printf("  [FAIL] %s\n", what);
        ++g_failed;
    }
}

int main() {
    std::printf("==========================================================\n");
    std::printf(" 248B 定长帧协议 —— 编译期契约复核\n");
    std::printf("==========================================================\n\n");

    // ---- 尺寸：这三条与头文件里的 static_assert 是同一件事，这里再跑一遍 ----
    std::printf("尺寸检查:\n");
    check(sizeof(BBox) == 24, "BBox = 24 字节");
    check(sizeof(DetectionHeader) == 8, "DetectionHeader = 8 字节");
    check(sizeof(FrameResult) == 248, "FrameResult = 248 字节 (8 + 10*24)");
    std::printf("         实测: BBox=%zu Header=%zu Frame=%zu\n",
                sizeof(BBox), sizeof(DetectionHeader), sizeof(FrameResult));

    // ---- 偏移：消费端直接按偏移读，顺序变了就等于协议变了 ----
    std::printf("\n字段偏移检查:\n");
    check(offsetof(FrameResult, header) == 0, "FrameResult.header @ 0");
    check(offsetof(FrameResult, boxes) == 8, "FrameResult.boxes  @ 8");
    check(offsetof(BBox, class_id) == 0, "BBox.class_id @ 0");
    check(offsetof(BBox, score) == 4, "BBox.score    @ 4");
    check(offsetof(BBox, x) == 8, "BBox.x        @ 8");
    check(offsetof(BBox, y) == 12, "BBox.y        @ 12");
    check(offsetof(BBox, w) == 16, "BBox.w        @ 16");
    check(offsetof(BBox, h) == 20, "BBox.h        @ 20");

    // ---- 别名必须与本体同尺寸，否则 patrol_daemon 会在不知情下读到别的东西 ----
    std::printf("\n历史别名检查:\n");
    check(sizeof(PatrolFrame) == sizeof(FrameResult), "PatrolFrame 与 FrameResult 同尺寸");
    check(sizeof(PatrolBBox) == sizeof(BBox), "PatrolBBox 与 BBox 同尺寸");
    check(sizeof(PatrolHeader) == sizeof(DetectionHeader),
          "PatrolHeader 与 DetectionHeader 同尺寸");

    // ---- 真造一帧，按消费端的读法读回来，确认能对上 ----
    //      只验尺寸/偏移还不够：pack(1) 写错成 pack(4) 时尺寸可能碰巧还是 248，
    //      但跨端会错位。这里做一次端到端的字节级往返。
    std::printf("\n字节级往返检查:\n");
    FrameResult frame;
    std::memset(&frame, 0, sizeof(frame));
    frame.header.frame_id   = 123456u;
    frame.header.bbox_count = 2u;
    frame.boxes[0].class_id = 0;      // person
    frame.boxes[0].score    = 0.813f;
    frame.boxes[0].x = 100.0f;
    frame.boxes[0].y = 200.0f;
    frame.boxes[0].w = 50.0f;
    frame.boxes[0].h = 120.0f;
    frame.boxes[1].class_id = 2;      // car
    frame.boxes[1].score    = 0.500f;
    frame.boxes[1].x = 640.0f;
    frame.boxes[1].y = 360.0f;
    frame.boxes[1].w = 80.0f;
    frame.boxes[1].h = 60.0f;

    // 模拟"发出去"：拿到原始字节
    unsigned char wire[248];
    std::memcpy(wire, &frame, sizeof(frame));

    // 模拟"对端按协议读"：直接从字节偏移取，不碰原结构体
    uint32_t wire_frame_id, wire_count;
    std::memcpy(&wire_frame_id, wire + 0, 4);
    std::memcpy(&wire_count,    wire + 4, 4);
    check(wire_frame_id == 123456u, "frame_id 从字节偏移 0 读回正确");
    check(wire_count == 2u, "bbox_count 从字节偏移 4 读回正确");

    // 第 0 个框起始于 8 字节处，class_id@+0 score@+4 x@+8 y@+12 w@+16 h@+20
    int32_t r_cid;
    float r_score, r_x, r_y, r_w, r_h;
    const unsigned char* b0 = wire + 8;
    std::memcpy(&r_cid,   b0 + 0,  4);
    std::memcpy(&r_score, b0 + 4,  4);
    std::memcpy(&r_x,     b0 + 8,  4);
    std::memcpy(&r_y,     b0 + 12, 4);
    std::memcpy(&r_w,     b0 + 16, 4);
    std::memcpy(&r_h,     b0 + 20, 4);
    check(r_cid == 0, "第 0 框 class_id 偏移正确");
    check(r_score > 0.812f && r_score < 0.814f, "第 0 框 score 偏移正确");
    check(r_x == 100.0f && r_y == 200.0f, "第 0 框 x/y 偏移正确");
    check(r_w == 50.0f && r_h == 120.0f, "第 0 框 w/h 偏移正确");

    // 第 1 个框起始于 8 + 24 = 32 字节处
    int32_t r1_cid;
    float r1_x;
    const unsigned char* b1 = wire + 8 + 24;
    std::memcpy(&r1_cid, b1 + 0, 4);
    std::memcpy(&r1_x,   b1 + 8, 4);
    check(r1_cid == 2, "第 1 框起始于偏移 32（步长 24 正确）");
    check(r1_x == 640.0f, "第 1 框 x 偏移正确");

    // 最后一帧的边界：第 9 个框必须完整落在 248 字节之内
    const size_t last_box_end = 8 + 9 * 24 + 24;
    check(last_box_end == 248, "第 9 框（最后一个）正好结束于 248 字节");

    std::printf("\n==========================================================\n");
    if (g_failed == 0) {
        std::printf(" 全部通过：248B 协议契约成立\n");
        std::printf("==========================================================\n");
        return 0;
    }
    std::printf(" ✗ %d 项失败：协议已漂移，改动方必须同步所有对端\n", g_failed);
    std::printf("==========================================================\n");
    return 1;
}
