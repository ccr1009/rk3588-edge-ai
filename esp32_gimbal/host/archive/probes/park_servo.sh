#!/bin/bash
# park_servo.sh — 收工前把舵机停掉并回中，释放 USB
#
# 为什么需要：D 1（连续摆动）是固件里的持久状态，
# 不显式发 D 0 它就会一直摆下去。下班前顺手复位一下。
cd /root/esp32_gimbal || exit 1

/root/rknn_env/bin/python - <<'PYEOF'
import sys, time
sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial

try:
    s = UsbCdcSerial(timeout=0.4)
except Exception as e:
    print("打不开 ESP32（可能桥还占着）: %s" % e)
    sys.exit(1)

s.reset_input_buffer()
for c in ["D 0", "H", "P 0 0"]:
    s.write((c + "\n").encode())
    s.flush()
    time.sleep(0.5)

s.write(b"I\n")
s.flush()
time.sleep(1.0)
line = s.readline().decode("utf-8", "replace").strip()
print("最终状态: %s" % (line or "(无回应)"))
s.close()
PYEOF
