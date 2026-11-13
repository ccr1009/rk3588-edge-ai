#!/bin/bash
# 重启桥接服务（ESP32 重新烧录/插拔后必须重启，否则 USB 句柄失效）
for pid in $(pgrep -f 'gimbal_bridge.py' 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
done
sleep 2

cd /root/esp32_gimbal || exit 1
nohup /root/rknn_env/bin/python gimbal_bridge.py --port 9100 > /tmp/bridge.log 2>&1 &
sleep 5

echo "=== 桥接日志 ==="
cat /tmp/bridge.log
echo
echo "=== 端口 ==="
ss -lnt 2>/dev/null | grep 9100 || echo "9100 没监听"
