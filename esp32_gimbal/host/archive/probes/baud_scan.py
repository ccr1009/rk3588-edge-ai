#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""波特率扫描：收到的数据是乱码，说明波特率不匹配。
逐个试常见波特率，看哪个能解出可读文本。"""
import serial
import time

PORT = "/dev/ttyUSB0"
BAUDS = [115200, 9600, 74880, 57600, 38400, 19200, 230400, 460800, 921600]

print("=" * 68)
print("波特率扫描 —— 找哪个波特率能解出可读文本")
print("=" * 68)
print()

for baud in BAUDS:
    try:
        s = serial.Serial(PORT, baud, timeout=0.5)
        time.sleep(0.3)
        s.reset_input_buffer()
        time.sleep(1.5)
        d = s.read(2000)
        s.close()
    except Exception as e:
        print("%-8d 打不开: %s" % (baud, e))
        continue

    if not d:
        print("%-8d 无数据" % baud)
        continue

    # 判断可读性：统计可打印 ASCII 的比例
    printable = sum(1 for b in d if 32 <= b < 127 or b in (9, 10, 13))
    ratio = printable / len(d)

    # 找有没有关键字
    has_boot = b"BOOT gimbal" in d or b"ESP-ROM" in d or b"boot:" in d
    has_reply = b"S " in d or b"OK" in d or b"ERR" in d

    sample = d[:80]
    try:
        txt = sample.decode("utf-8", "replace")
    except Exception:
        txt = repr(sample)

    print("%-8d %5d字节  可读率%.0f%%  BOOT=%s  协议回包=%s"
          % (baud, len(d), ratio * 100,
             "有" if has_boot else "无",
             "有" if has_reply else "无"))
    print("         样本: %r" % txt[:70])
    print()

print("=" * 68)
