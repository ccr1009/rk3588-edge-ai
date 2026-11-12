#!/bin/bash
# 评估能否编译 cdc_acm 模块
echo "=== 1. 内核头文件环境 ==="
KVER=$(uname -r)
BUILD=/lib/modules/$KVER/build
echo "  build 路径: $BUILD"
ls -ld $BUILD 2>&1
echo "  --- 是否有 .config ---"
ls -l $BUILD/.config 2>&1
echo "  --- 是否有 Module.symvers ---"
ls -l $BUILD/Module.symvers 2>&1
echo "  --- include 目录 ---"
ls -d $BUILD/include 2>&1

echo
echo "=== 2. 有没有内核源码树 ==="
ls -d /usr/src/*/ 2>/dev/null | head -5
echo "  --- 找 drivers/usb/class ---"
find /usr/src -type d -name class -path '*usb*' 2>/dev/null | head -3

echo
echo "=== 3. 有没有 gcc 和 make ==="
command -v gcc && gcc --version | head -1
command -v make && make --version | head -1

echo
echo "=== 4. 当前内核模块目录结构 ==="
ls /lib/modules/$KVER/ 2>&1
echo "  --- kernel/drivers/usb/class ---"
ls /lib/modules/$KVER/kernel/drivers/usb/class/ 2>&1 | head -10

echo
echo "=== 5. 判断 ==="
if [ -f "$BUILD/.config" ] && [ -f "$BUILD/Module.symvers" ]; then
    echo "  ✅ 环境完整，可以尝试编译 cdc_acm 模块"
elif [ -f "$BUILD/.config" ]; then
    echo "  ⚠️  有 .config 但缺 Module.symvers（编译可能报警告但可行）"
else
    echo "  ❌ 缺关键文件，编译模块困难"
fi
