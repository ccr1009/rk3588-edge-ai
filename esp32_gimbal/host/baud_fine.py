#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""估算实际波特率：用非标准波特率逐个试，找到可读率最高的那个。

思路：
    已知 ESP32 打印的启动日志是纯 ASCII 文本（BOOT gimbal v2 等）。
    如果波特率对，可读率会接近 100%。
    之前测的 9 个标准波特率都可读率 <5%，说明可能是"非标准"的，
    或者根本不是文本（是别的协议）。

    这里用更细的扫描范围，并且每次采更长时间。
"""
import time

import serial

PORT = "/dev/ttyUSB0"

# 常见 + 非标准波特率，范围覆盖广一些
BAUDS = [
    1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 56000, 57600,
    76800, 115200, 128000, 230400, 250000, 256000, 460800, 500000,
    921600, 1000000, 1500000, 2000000, 3000000,
]

print("=" * 72)
print("精细波特率扫描（每个采 2 秒）")
print("=" * 72)
print()

best = []
for baud in BAUDS:
    try:
        s = serial.Serial(PORT, baud, timeout=0.3)
        time.sleep(0.2)
        s.reset_input_buffer()
        time.sleep(2.0)
        d = s.read(4000)
        s.close()
    except Exception as e:
        continue

    if not d:
        continue

    printable = sum(1 for b in d if 32 <= b < 127 or b in (9, 10, 13))
    ratio = printable / len(d)
    has_text = any(k in d for k in [b"BOOT", b"gimbal", b"READY", b"ESP-ROM", b"boot:"])
    best.append((ratio, baud, len(d), has_text, d[:60]))

best.sort(reverse=True)

print("%-10s %-10s %-10s %-8s" % ("波特率", "可读率", "字节数", "有关键字"))
print("-" * 72)
for ratio, baud, n, has_text, sample in best[:12]:
    print("%-10d %-10s %-10d %-8s" % (baud, "%.0f%%" % (ratio * 100), n,
                                       "是" if has_text else "否"))
    if ratio > 0.3:
        print("    样本: %r" % sample)

print()
print("=" * 72)
if best and best[0][0] > 0.5:
    print("✅ 找到可读的波特率: %d (可读率 %.0f%%)" % (best[0][1], best[0][0] * 100))
    print("   固件实际用的不是 115200，需要用这个值！")
elif best and best[0][0] > 0.2:
    print("⚠️  最高可读率只有 %.0f%% (%d baud)，部分可读但不完整"
          % (best[0][0] * 100, best[0][1]))
    print("   可能是信号质量问题，或者波特率接近但不对")
else:
    print("❌ 所有波特率都解不出文本")
    print("   说明收到的不是 UART 文本数据，可能是：")
    print("   1) 信号线接到了别的引脚（不是 GPIO43 TX）")
    print("   2) 拾取的是电磁噪声，不是真实数据")
    print("   3) ESP32 的日志走的是 USB 口，UART0 没有输出")
print("=" * 72)
