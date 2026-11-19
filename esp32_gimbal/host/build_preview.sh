#!/bin/bash
# build_preview.sh —— 板上原生编译"云台视觉追踪 + 实时预览"一体化程序
#
# 2026-11-19 变更：本程序现在 #include "edgevision_protocol.h"
# （248B 帧协议唯一真源），-I 里必须带上 /root/lab。
# 头文件同步：
#   scp lab/include/edgevision_protocol.h rk3588:/root/lab/
cd /root/lab || exit 1

if [ ! -f /root/lab/edgevision_protocol.h ]; then
    echo "[-] 缺少 /root/lab/edgevision_protocol.h（248B 协议真源）"
    echo "    先同步：scp lab/include/edgevision_protocol.h rk3588:/root/lab/"
    exit 1
fi

g++ -O2 -std=c++17 w4_edgevision_gimbal_preview.cpp \
    /root/edgevision/deploy/cpp/src/postprocess_dfl_free.cpp \
    -o w4_edgevision_gimbal_preview \
    -I/root/lab \
    -I/root/edgevision/deploy/cpp/include \
    -I/root/edgevision/deploy/cpp/3rdparty/rknn_api/include \
    $(pkg-config --cflags --libs opencv4) \
    -lrknnrt -lpthread 2>&1 | head -40
rc=${PIPESTATUS[0]}
if [ "$rc" -eq 0 ] && [ -x w4_edgevision_gimbal_preview ]; then
    echo "[+] 编译成功"
    ls -lh w4_edgevision_gimbal_preview
else
    echo "[-] 编译失败 rc=$rc"
    exit 1
fi
