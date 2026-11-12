#!/bin/bash
# 停止追踪 + 回中 + 验证舵机还能动（不要把云台留在限位上）
pkill -9 -f w4_edgevision_gimbal_preview 2>/dev/null
sleep 1
pgrep -f w4_edgevision_gimbal_preview >/dev/null 2>&1 && echo "还在跑" || echo "已停止 ✓"

/root/rknn_env/bin/python - <<'PY'
import serial, time
s = serial.Serial("/dev/ttyUSB0", 115200, timeout=1.0)
time.sleep(0.3); s.reset_input_buffer()

def send(c, w=1.0):
    s.write((c + "\n").encode()); s.flush(); time.sleep(w)
    return s.read(300).decode(errors="replace").strip()

print("回中:", send("H", 1.2))
print("状态:", send("Q", 0.5))
print()
print("小角度测试（看舵机是否还能动）：")
for p in (-20, 0, 20, 0):
    print("  P %+3d 0 -> %s" % (p, send("P %d 0" % p, 1.0)))
print()
print("最终状态:", send("Q", 0.5))
s.close()
PY
