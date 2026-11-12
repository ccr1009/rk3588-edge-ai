#!/bin/bash
# 检查 cdc_acm 驱动的三种可能存在形式
#
# 内核没编 CONFIG_USB_ACM=y（内置），但可能：
#   ① 编成了模块（.ko 文件），能 modprobe 加载
#   ② 系统里根本没有这个驱动
#   ③ 有别的替代驱动

KVER=$(uname -r)
echo "内核版本: $KVER"
echo

echo "=== 1. 内核配置里 ACM 相关 ==="
zcat /proc/config.gz 2>/dev/null | grep -i -E 'USB_ACM|CDC_ACM|USB_CDC' || \
    grep -i -E 'USB_ACM|CDC_ACM|USB_CDC' /boot/config-$KVER 2>/dev/null || \
    echo "  (读不到内核配置)"

echo
echo "=== 2. 系统里有没有 cdc_acm.ko 模块文件 ==="
find /lib/modules/$KVER -name "*cdc*" -o -name "*acm*" 2>/dev/null | head -10
echo "  ---"
find / -name "cdc_acm.ko*" 2>/dev/null | head -5
echo "  (以上为空 = 系统里没有这个模块文件)"

echo
echo "=== 3. 当前已加载的 USB 类驱动 ==="
ls /sys/bus/usb/drivers/ | head -20

echo
echo "=== 4. 系统有没有内核源码/头文件（能自己编模块）==="
ls -d /usr/src/linux-headers-* 2>/dev/null | head -3
ls /lib/modules/$KVER/build 2>/dev/null
echo "  (为空 = 不能自己编模块)"

echo
echo "=== 5. ESP32 设备的 USB 描述符 ==="
DEV=$(ls -d /sys/bus/usb/devices/1-1.2 2>/dev/null)
if [ -n "$DEV" ]; then
    echo "  路径: $DEV"
    for f in idVendor idProduct bDeviceClass bNumInterfaces bNumConfigurations; do
        [ -e "$DEV/$f" ] && echo "  $f = $(cat $DEV/$f)"
    done
    echo
    echo "  --- 各接口 ---"
    for iface in $DEV/*:*/; do
        i=$(basename "$iface")
        cls=$(cat "$iface/bInterfaceClass" 2>/dev/null)
        sub=$(cat "$iface/bInterfaceSubClass" 2>/dev/null)
        proto=$(cat "$iface/bInterfaceProtocol" 2>/dev/null)
        drv=$(basename "$(readlink -f "$iface/driver" 2>/dev/null)" 2>/dev/null)
        echo "  $i  class=$cls sub=$sub proto=$proto  driver=${drv:-none}"
    done
else
    echo "  没找到 ESP32 设备"
fi

echo
echo "=== 6. 结论 ==="
if zcat /proc/config.gz 2>/dev/null | grep -q 'CONFIG_USB_ACM is not set'; then
    echo "  内核明确没编 USB_ACM"
fi
if find /lib/modules/$KVER -name "cdc_acm*" 2>/dev/null | grep -q .; then
    echo "  ✅ 找到了 cdc_acm 模块文件，可以 modprobe 加载"
else
    echo "  ❌ 系统里没有 cdc_acm 模块文件"
    echo "     → 需要走 userspace 方案（pyusb 直接驱动）"
fi
