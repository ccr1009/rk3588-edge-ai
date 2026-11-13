#!/bin/bash
# stop_tilt_only.sh — 停止单轴追踪，恢复引脚默认，收工
for pid in $(pgrep -f 'w4_edgevision_gimbal_preview' 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
    echo "杀掉 preview PID=$pid"
done
for pid in $(pgrep -f 'gimbal_bridge.py' 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
    echo "杀掉 bridge PID=$pid"
done
sleep 2

echo
echo "恢复引脚默认（PAN→14, TILT→15）并回中..."
cat > /tmp/_restore.py <<'PYEOF'
import sys, time
sys.path.insert(0, "/root/esp32_gimbal")
try:
    from usb_cdc_serial import UsbCdcSerial
    s = UsbCdcSerial(timeout=0.5)
    s.reset_input_buffer()
    def cmd(c, w=0.8):
        s.write((c + "\n").encode()); s.flush()
        t0 = time.time()
        while time.time() - t0 < w:
            l = s.readline()
            if l:
                v = l.decode("utf-8", "replace").strip()
                if v: return v
        return None
    for c in ["D 0", "A"]:
        cmd(c, 0.4)
    time.sleep(0.3)
    cmd("G 14", 0.8)
    cmd("T 15", 0.8)
    cmd("P 0 0", 0.5)
    print("  最终: %s" % cmd("I", 1.0))
    s.close()
except Exception as e:
    print("  跳过（%s）" % e)
PYEOF
/root/rknn_env/bin/python /tmp/_restore.py

echo
echo "已停止 ✓"
