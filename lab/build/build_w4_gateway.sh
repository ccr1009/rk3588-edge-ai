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
#   scp lab/include/edgevision_protocol.h rk3588:/root/lab/
#   然后板上执行本脚本。
#
# 2026-11-19 变更：网关现在 #include "edgevision_protocol.h"（248B 帧协议唯一真源），
# 所以 -I/root/lab 是必需的，头文件不传上去就会编译失败。
set -e
echo "[*] 开始编译 EdgeVision 全链路网关服务..."

if [ ! -f /root/lab/edgevision_protocol.h ]; then
    echo "✗ 缺少 /root/lab/edgevision_protocol.h（248B 协议真源）"
    echo "  先同步：scp lab/include/edgevision_protocol.h rk3588:/root/lab/"
    exit 1
fi

g++ -O2 /root/lab/w4_edgevision_gateway.cpp -o /root/lab/w4_edgevision_gateway \
    -I/root/lab \
    -I/usr/include/opencv4 \
    -I/root/edgevision/deploy/cpp/3rdparty/rknn_api/include \
    -lopencv_core -lopencv_videoio -lopencv_imgproc \
    -lrknnrt -lpthread

echo "[+] 编译成功: /root/lab/w4_edgevision_gateway"
ls -lh /root/lab/w4_edgevision_gateway
