#!/bin/bash
# USB 串口彻底复位：用 sysfs 强制解绑/重绑 ch341 驱动
#
# 为什么需要这个：
#   出现 "failed to send control message: -32" (EPIPE) 后，
#   /dev/ttyUSB0 节点还在、lsusb 也能看到设备，但内核端点已经失效，
#   单纯的 close/open 恢复不了。必须让驱动重新枚举。
#
# 原理：把 1a86:7523 从 ch341 驱动解绑，再绑回去，
#       内核会重新走一遍 probe → 重新建立端点。

echo "=== 1. 当前状态 ==="
lsusb | grep 1a86 || echo "  CH340 不在"
ls -l /dev/ttyUSB0 2>&1

echo
echo "=== 2. 找到 sysfs 路径 ==="
DEV=$(ls -d /sys/bus/usb/drivers/ch341/1-1.*:1.0 2>/dev/null | head -1)
if [ -z "$DEV" ]; then
    echo "  没找到 ch341 绑定的接口"
    echo "  尝试从 ttyUSB0 反查..."
    DEV=$(readlink -f /sys/class/tty/ttyUSB0/device 2>/dev/null)
    echo "  $DEV"
else
    echo "  $DEV"
fi

echo
echo "=== 3. 解绑 ch341 驱动 ==="
IFACE=$(basename "$DEV")
echo "  解绑 $IFACE"
echo "$IFACE" > /sys/bus/usb/drivers/ch341/unbind 2>&1 && echo "  解绑成功" || echo "  解绑失败"

sleep 2

echo
echo "=== 4. 重新绑定 ==="
echo "$IFACE" > /sys/bus/usb/drivers/ch341/bind 2>&1 && echo "  重绑成功" || echo "  重绑失败"

sleep 3

echo
echo "=== 5. 结果 ==="
lsusb | grep 1a86 || echo "  CH340 不在"
ls -l /dev/ttyUSB0 2>&1
echo
echo "  dmesg:"
dmesg | tail -6

echo
echo "=== 6. 测试能否打开 ==="
/root/rknn_env/bin/python /root/esp32_gimbal/_probe_open.py 2>&1 | tail -2
