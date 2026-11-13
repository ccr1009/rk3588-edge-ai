#!/bin/bash
# who_changed_pins.sh — 查清 pan 引脚为什么从 14 变成了 40
cd /root/esp32_gimbal || exit 1

echo "======== 1. 当前引脚 ========"
cat > /tmp/_p1.py <<'PYEOF'
import sys, time
sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial
s = UsbCdcSerial(timeout=0.5)
s.reset_input_buffer()
s.write(b"I\n"); s.flush()
t0 = time.time()
while time.time() - t0 < 2.0:
    l = s.readline()
    if l:
        v = l.decode("utf-8", "replace").strip()
        if v:
            print("  I ->", v)
            break
s.close()
PYEOF
/root/rknn_env/bin/python /tmp/_p1.py 2>&1

echo
echo "======== 2. 有没有残留的 python 进程在操作 USB ========"
ps aux | grep -E 'python.*(gimbal|usb_cdc|bridge|servo|diag|definitive)' | grep -v grep || echo "  (无)"

echo
echo "======== 3. 桥接进程 ========"
pgrep -af gimbal_bridge || echo "  (无)"

echo
echo "======== 4. 谁最近碰过 GPIO 相关的脚本 ========"
ls -lt /root/esp32_gimbal/*.py /root/lab/* 2>/dev/null | head -12

echo
echo "======== 5. 固件里 G 命令的调用者（搜索代码）========"
grep -rn 'set_pan_pin\|"G \|G %d\|_send_raw("G\|send.*G ' /root/esp32_gimbal/*.py 2>/dev/null | head -20 || echo "  (没找到)"

echo
echo "======== 6. 一体化程序 / 追踪程序是否在跑 ========"
pgrep -af 'w4_edgevision|gimbal_preview|gimbal_tracker' || echo "  (无)"

echo
echo "======== 7. 固件启动信息（看默认引脚）========"
echo "  固件默认 PAN_PIN_DEFAULT = 14（见 gimbal_main.c 第 69 行）"
echo "  如果 I 回的是 40，说明运行时有命令改过它"
