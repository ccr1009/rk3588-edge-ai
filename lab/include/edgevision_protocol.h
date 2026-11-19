#ifndef EDGEVISION_PROTOCOL_H
#define EDGEVISION_PROTOCOL_H

// ===========================================================================
//  9000 端口定长帧协议 —— 全系统唯一真源
// ---------------------------------------------------------------------------
//  这份头文件存在的唯一理由：**248 字节帧的布局只允许有一处定义。**
//
//  为什么要把这件事当回事（不是洁癖，是踩过的坑）：
//    在这之前，struct FrameResult 在 C++ 里被**复制了 4 份**：
//      · lab/vision/edgevision_gateway.cpp            发送端（事实上的真源）
//      · lab/agent/rkllm_embodied_agent.cpp           消费端
//      · lab/agent/edgevision_agent_server.cpp        消费端
//      · lab/agent/edgevision_patrol_daemon.cpp       消费端（还另起了名字）
//      · esp32_gimbal/host/w4_edgevision_gimbal_preview.cpp  消费端
//    只改其中一处的后果**不是编译报错，而是解析出偏移错位的框** ——
//    画面看着"像那么回事"，但框贴在错误的位置上。
//    本项目已经因为"假数据看起来像真的"栽过两次（见 TROUBLESHOOTING 第 6 条），
//    这种静默错位是同一个坑的潜伏形态。
//
//  怎么保证不再漂：本文件末尾的 static_assert 是编译期契约。
//    任何一端改了字段顺序/类型/数量，**这里立刻编不过**，而不是等联调时才发现。
//
//  对端：
//    · Python 侧参考实现 lab/tests/pc_client_viewer.py（手工对齐，未共用本头文件）
//    · 学习线 learn/w3_网络/*.cpp 与 tools/d20_gateway_npu.cpp **故意不同构**
//      （BBox 数量分别是 2 和 8，是早期练习），不要把它们并进来。
// ===========================================================================

#include <cstdint>
#include <cstddef>

// 一帧最多携带的检测目标数。
//   =10 不是随便定的：它与发送端的推流队列深度、Python 侧的 MAX_BBOXES
//   以及历史 248 字节口径全部绑定。改这个数字等于改协议版本，
//   必须同时改所有对端并重新跑两端联调。
static constexpr uint32_t kMaxBBoxes = 10;

#pragma pack(push, 1)

// 单个检测框，24 字节
struct BBox {
    int32_t class_id;    // 0~79：COCO 80 类
    float   score;       // 置信度 0.0~1.0
    float   x, y, w, h;  // 还原到摄像头原始画面的像素坐标（已做 letterbox 逆变换）
};

// 帧头，8 字节
struct DetectionHeader {
    uint32_t frame_id;    // 单调递增，消费端靠它判新鲜度
    uint32_t bbox_count;  // 实际检出目标数，0 ~ kMaxBBoxes
};

// 整帧，248 字节（8 + 10*24）
struct FrameResult {
    DetectionHeader header;
    BBox            boxes[kMaxBBoxes];
};

#pragma pack(pop)

// ---------------------------------------------------------------------------
//  编译期契约 —— 这三行是这个头文件存在的主要价值
// ---------------------------------------------------------------------------
static_assert(sizeof(BBox)            == 24,  "BBox 必须是 24 字节：协议对端按此偏移解析");
static_assert(sizeof(DetectionHeader) == 8,   "DetectionHeader 必须是 8 字节");
static_assert(sizeof(FrameResult)     == 248, "整帧必须是 248 字节（8 + 10*24）");

static_assert(offsetof(FrameResult, header)    == 0,   "header 必须在偏移 0");
static_assert(offsetof(FrameResult, boxes)     == 8,   "boxes 必须在偏移 8");
static_assert(offsetof(BBox, class_id)         == 0,   "class_id 必须在偏移 0");
static_assert(offsetof(BBox, score)            == 4,   "score 必须在偏移 4");
static_assert(offsetof(BBox, x)                == 8,   "x 必须在偏移 8");
static_assert(offsetof(BBox, w)                == 16,  "w 必须在偏移 16");

// ---------------------------------------------------------------------------
//  历史别名
// ---------------------------------------------------------------------------
//  lab/agent/edgevision_patrol_daemon.cpp 在引入本头文件之前，把同一套结构
//  命名为 PatrolBBox / PatrolHeader / PatrolFrame。名字不同、布局相同。
//  这里保留别名，让它的业务代码一行都不用改 —— 减少这次重构的爆炸半径。
using PatrolBBox   = BBox;
using PatrolHeader = DetectionHeader;
using PatrolFrame  = FrameResult;

static_assert(sizeof(PatrolFrame) == 248, "PatrolFrame 别名必须与 FrameResult 同尺寸");

#endif  // EDGEVISION_PROTOCOL_H
