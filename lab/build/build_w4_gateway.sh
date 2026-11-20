#!/bin/bash
# 板上原生编译 9000 推流网关（edgevision.service 用的就是这个二进制）
#
# ⚠️ 版本说明（2026-11-02 重命名后）：
#   仓库里的源码叫 lab/vision/edgevision_gateway.cpp（去掉了误导性的 w4_ 前缀，
#   因为它不是第 4 周练习，而是全项目的 9000 网关主线）。
#   但**板上源码与二进制名保持 w4_ 前缀不变**，原因：
#     - edgevision.service 的 ExecStart=/root/lab/w4_edgevision_gateway
#     - 多个排查脚本（tools/npu_doctor.sh、phase0*.sh）用 pgrep -f w4_edgevision_gateway
#       匹配进程名；改二进制名会让这些脚本失效
#   所以本脚本的输入路径写的是板端真实文件名，和仓库文件名不要求一致。
#
# 部署方式（把仓库源码传到板上）：
#   scp lab/vision/edgevision_gateway.cpp rk3588:/root/lab/w4_edgevision_gateway.cpp
#   然后板上执行本脚本。
set -e
echo "[*] 开始编译 EdgeVision 全链路网关服务..."

# ⚠️ 2026-11-20 修：本脚本原版缺两样东西，照它编译会出一个"看起来正常、
#    实际没跑 NPU"的二进制 —— 属于 AGENTS.md 第①条（假数据）的变种形态：
#
#   ① 缺 `-DENABLE_RKNN`：deploy/cpp/src/rknn_engine_dfl_free.cpp 里所有真
#      RKNN 调用都包在 `#ifdef ENABLE_RKNN` 内，没定义就编成 x86 桩，
#      启动日志打 `[!] x86 Stub Mode (no NPU)`、NPU 耗时恒 0ms、
#      推流出去全是"检出 0 目标"。画面/端口/协议全都正常，只有结果是假的。
#      CMakeLists.txt:128 里一直有这个宏，本脚本是手写命令，漏抄了。
#
#   ② 缺引擎与后处理源文件：`undefined reference to RKNNEngineDflFree::...`
#      链接失败。它们不在 /root/lab，在 /root/edgevision/deploy/cpp/src/。
#
#   验证方法（编完必查，两条都要过）：
#      strings 二进制 | grep -c "x86 Stub Mode"   → 必须 0
#      nm -C  二进制 | grep -c  cam_read          → 必须 ≥1（断帧加固在）
g++ -O2 -DENABLE_RKNN /root/lab/w4_edgevision_gateway.cpp \
    /root/edgevision/deploy/cpp/src/rknn_engine_dfl_free.cpp \
    /root/edgevision/deploy/cpp/src/postprocess_dfl_free.cpp \
    -o /root/lab/w4_edgevision_gateway \
    -I/usr/include/opencv4 \
    -I/root/edgevision/deploy/cpp/include \
    -I/root/edgevision/deploy/cpp/3rdparty/rknn_api/include \
    -L/root/edgevision/deploy/cpp/3rdparty/rknn_api/lib \
    -lopencv_core -lopencv_videoio -lopencv_imgproc \
    -lrknnrt -lpthread

if strings /root/lab/w4_edgevision_gateway | grep -q "x86 Stub Mode"; then
    echo "[-] 致命：编出的是 x86 桩，没有真 NPU。检查 -DENABLE_RKNN。"
    exit 1
fi

echo "[+] 编译成功: /root/lab/w4_edgevision_gateway"
ls -lh /root/lab/w4_edgevision_gateway
