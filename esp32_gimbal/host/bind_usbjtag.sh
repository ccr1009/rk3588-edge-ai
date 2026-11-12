#!/bin/bash
# 把 ESP32 原生 USB-JTAG (303a:1001) 强绑到 usbserial_generic，看能不能出 ttyUSB
#
# 背景：
#   EVM3588-B 内核没编 CONFIG_USB_ACM，ESP32 的 CDC-ACM 类设备建不出 /dev/ttyACM0。
#   README 里记着"用 usbserial_generic 强绑能短暂出 ttyUSB0，但驱动不发
#   SET_LINE_CODING / SET_CONTROL_LINE_STATE，通信不稳定"。
#
#   现在 TTL 那路不通，这可能是唯一能用的通道。值得再试一次。

echo "=== 1. 确认设备存在 ==="
lsusb | grep -E '303a:1001' || { echo "ESP32 原生 USB 不在"; exit 1; }

echo
echo "=== 2. 找到 sysfs 路径 ==="
DEV=$(ls -d /sys/bus/usb/devices/1-1.2:1.0 2>/dev/null || ls -d /sys/bus/usb/devices/*:1.0 2>/dev/null | head -1)
echo "  $DEV"

echo
echo "=== 3. 检查内核有没有 usbserial ==="
ls /sys/bus/usb-serial/drivers/ 2>&1

echo
echo "=== 4. 尝试强绑 usbserial_generic ==="
IFACE="1-1.2:1.0"
echo "  绑定 $IFACE"
if [ -e /sys/bus/usb/drivers/usbserial_generic/new_id ]; then
    # 方式一：通过 new_id 动态添加 VID/PID
    echo "  vendor=0x303a product=0x1001" > /sys/bus/usb/drivers/usbserial_generic/new_id 2>&1
    echo "  new_id 方式: $?"
fi

if [ -e /sys/bus/usb/drivers/usbserial_generic/bind ]; then
    echo "$IFACE" > /sys/bus/usb/drivers/usbserial_generic/bind 2>&1
    echo "  bind 方式: $?"
fi

sleep 2

echo
echo "=== 5. 结果 ==="
ls -l /dev/ttyUSB* /dev/ttyACM* 2>&1
echo
echo "  dmesg:"
dmesg | tail -8
