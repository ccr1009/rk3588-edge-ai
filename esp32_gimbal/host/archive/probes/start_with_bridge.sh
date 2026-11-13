#!/bin/bash
# 启动一体化预览 + TCP 桥接到 USB CDC
set -u
CAM="${CAM_DEV:-/dev/video81}"

systemctl stop edgevision 2>/dev/null
for pid in $(pgrep -f 'w4_edgevision_gimbal_preview' 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
done
sleep 2

# 确认桥接在跑
if ! ss -lnt 2>/dev/null | grep -q ':9100'; then
    echo "[!] 桥接服务没在跑，正在启动..."
    cd /root/esp32_gimbal || exit 1
    nohup /root/rknn_env/bin/python gimbal_bridge.py --port 9100 > /tmp/bridge.log 2>&1 &
    sleep 4
fi

if ss -lnt 2>/dev/null | grep -q ':9100'; then
    echo "[+] 桥接服务就绪 (9100)"
else
    echo "[-] 桥接服务起不来"
    cat /tmp/bridge.log
    exit 1
fi

cd /root/lab || exit 1
nohup ./w4_edgevision_gimbal_preview "$CAM" --bridge 127.0.0.1:9100 --no-tilt --no-3d \
    > /tmp/gp5.log 2>&1 &
echo "[+] 预览已启动 PID=$!"
sleep 12
echo "=== 日志 ==="
head -14 /tmp/gp5.log
