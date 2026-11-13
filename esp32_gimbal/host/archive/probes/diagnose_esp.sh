#!/bin/bash
# diagnose_esp.sh — 排查 ESP32 USB 通不上的原因
cd /root/esp32_gimbal || exit 1

echo "======== lsusb ========"
lsusb | grep -iE '303a|1a86' || echo "(没找到 303a / 1a86 设备)"

echo
echo "======== 桥接进程 ========"
pgrep -af gimbal_bridge || echo "(无桥接进程)"

echo
echo "======== 9100 端口 ========"
ss -tln 2>/dev/null | grep 9100 || echo "(9100 未监听)"

echo
echo "======== dmesg 最近 USB 事件 ========"
dmesg 2>/dev/null | grep -iE 'usb.*(disconnect|reset|error|303a)' | tail -15 || echo "(读不到 dmesg)"

echo
echo "======== pyusb 枚举 ========"
cat > /tmp/_diag.py <<'PYEOF'
import sys
sys.path.insert(0, "/root/esp32_gimbal")
import usb.core
d = usb.core.find(idVendor=0x303A, idProduct=0x1001)
print("device:", d)
if d is not None:
    print("bus %s addr %s" % (d.bus, d.address))
    for cfg in d:
        for i in cfg:
            print("  iface %d class=0x%02x sub=0x%02x"
                  % (i.bInterfaceNumber, i.bInterfaceClass, i.bInterfaceSubClass))
PYEOF
/root/rknn_env/bin/python /tmp/_diag.py 2>&1

echo
echo "======== 打开 CDC 并收 2 秒（看有没有残留输出 / 复位信息）========"
cat > /tmp/_diag2.py <<'PYEOF'
import sys, time
sys.path.insert(0, "/root/esp32_gimbal")
try:
    from usb_cdc_serial import UsbCdcSerial
    ser = UsbCdcSerial(timeout=0.4)
    print("打开成功")
    ser.reset_input_buffer()
    t0 = time.time()
    got = []
    while time.time() - t0 < 2.0:
        l = ser.readline()
        if l:
            v = l.decode("utf-8", "replace").strip()
            if v:
                got.append(v)
    print("2 秒内收到 %d 行:" % len(got))
    for x in got[-10:]:
        print("   ", x)
    ser.close()
except Exception as e:
    print("打开失败:", e)
PYEOF
/root/rknn_env/bin/python /tmp/_diag2.py 2>&1
