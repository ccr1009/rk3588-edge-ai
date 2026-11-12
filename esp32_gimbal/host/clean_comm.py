#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""干净通信测试：清空缓冲后逐个命令交互，看回复是否完整。"""
import time

import serial

PORT = "/dev/ttyUSB0"

s = serial.Serial(PORT, 115200, timeout=0.5)
time.sleep(0.5)
# 关键：多清几次缓冲，把之前积累的噪声冲掉
s.reset_input_buffer()
time.sleep(0.3)
s.reset_input_buffer()
time.sleep(0.3)
s.reset_input_buffer()

print("=" * 64)
print("干净通信测试")
print("=" * 64)
print()

commands = ["Q", "I", "P 0 0", "Q", "H", "Q"]
for cmd in commands:
    s.write((cmd + "\n").encode())
    s.flush()
    time.sleep(0.8)
    # 读所有可用数据
    d = b""
    t0 = time.time()
    while time.time() - t0 < 0.5:
        chunk = s.read(256)
        if chunk:
            d += chunk
        else:
            time.sleep(0.02)

    printable = sum(1 for b in d if 32 <= b < 127 or b in (9, 10, 13))
    ratio = printable / len(d) if d else 0

    print("--> %-8r" % cmd)
    if not d:
        print("    <-- (无回复)")
    elif ratio > 0.8:
        # 干净文本
        txt = d.decode("utf-8", "replace").strip()
        print("    <-- %s   [可读率 %.0f%%]" % (txt, ratio * 100))
    else:
        # 有噪声，提取可读部分
        txt = "".join(chr(b) if 32 <= b < 127 else "" for b in d)
        print("    <-- %r   [可读率 %.0f%%, 有噪声]" % (txt, ratio * 100))
    print()

s.close()
print("=" * 64)
