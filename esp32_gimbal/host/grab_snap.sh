#!/bin/bash
# grab_snap.sh — 抓一帧网页快照，看看检测框长什么样
cd /root/esp32_gimbal || exit 1

for pid in $(pgrep -f 'w4_edgevision_gimbal_preview' 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
done
systemctl stop edgevision 2>/dev/null
sleep 1

# 确保桥接在（否则云台控制起不来，但不影响抓图）
if ! ss -tln 2>/dev/null | grep -q 9100; then
    bash /root/esp32_gimbal/restart_bridge.sh >/dev/null 2>&1
    sleep 4
fi

# 引脚映射
cat > /tmp/_rm.py <<'PYEOF'
import sys, time
sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial
try:
    s = UsbCdcSerial(timeout=0.5)
    s.reset_input_buffer()
    for c in ["D 0", "A", "G 15", "T 14", "P 0 0"]:
        s.write((c + "\n").encode()); s.flush(); time.sleep(0.3)
    s.close()
except Exception:
    pass
PYEOF
bash /root/esp32_gimbal/stop_bridge.sh >/dev/null 2>&1
sleep 1
/root/rknn_env/bin/python /tmp/_rm.py >/dev/null 2>&1
bash /root/esp32_gimbal/restart_bridge.sh >/dev/null 2>&1
sleep 3

cd /root/lab || exit 1
nohup ./w4_edgevision_gimbal_preview /dev/video81 \
    --bridge 127.0.0.1:9100 --no-pan --no-3d \
    --kp-tilt 0.10 --settle 5 \
    > /tmp/snap_run.log 2>&1 &
PID=$!
echo "启动 PID=$PID，等 10 秒..."
sleep 10

if ! kill -0 "$PID" 2>/dev/null; then
    echo "❌ 程序退出了，日志："
    cat /tmp/snap_run.log
    exit 1
fi

echo
echo "=== 抓快照 ==="
for i in 1 2 3; do
    curl -s -o /tmp/snap_$i.jpg http://127.0.0.1:8080/snapshot
    sz=$(stat -c%s /tmp/snap_$i.jpg 2>/dev/null || echo 0)
    echo "  snap_$i.jpg  ${sz} bytes"
    sleep 1
done

echo
echo "=== 最近 6 行日志（看状态是 LOCK/TRACK/LOST/SATURATED）==="
tail -6 /tmp/snap_run.log

echo
echo "=== 状态统计 ==="
grep -oE '(LOCK|TRACK|LOST|SATURATED)' /tmp/snap_run.log | sort | uniq -c
