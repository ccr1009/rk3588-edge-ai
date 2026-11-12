#!/bin/bash
cd /root/lab || exit 1
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
