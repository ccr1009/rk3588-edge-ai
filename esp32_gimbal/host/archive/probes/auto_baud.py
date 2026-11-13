#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动波特率探测：每次换波特率后等待，期间按 RST 就能触发输出。

用法：
    python3 auto_baud.py

交互：脚本会提示"现在请按 RST"，你按一下，脚本继续。
"""
import time

import serial

PORT = "/dev/ttyUSB0"
BAUDS = [115200, 9600, 74880, 57600, 38400, 19200, 230400, 460800]

print("=" * 70)
print("自动波特率探测")
print("=" * 70)
print()
print("提示：脚本每轮会等你 6 秒，请在这 6 秒内按一下 ESP32 的 RST 键。")
print("      什么时候按都可以，只要在窗口内。")
input("准备好了按回车开始...")
print()

found = []
for baud in BAUDS:
    try:
        s = serial.Serial(PORT, baud, timeout=0.1)
        s.reset_input_buffer()
    except Exception as e:
        print("%-8d 打不开: %s" % (baud, e))
        continue

    print("--- 波特率 %d ---" % baud, end=" ", flush=True)
    t0 = time.time()
    n = 0
    best_text = b""
    while time.time() - t0 < 6.0:
        d = s.read(512)
        if d:
            n += len(d)
            p = sum(1 for b in d if 32 <= b < 127 or b in (9, 10, 13))
            if p / len(d) > 0.5 and len(d) > len(best_text):
                best_text = d
    s.close()

    if n == 0:
        print("无数据")
    elif best_text:
        print("%d 字节，可读: %r" % (n, best_text[:80]))
        found.append((baud, n, best_text))
    else:
        print("%d 字节，全是噪声" % n)

print()
print("=" * 70)
if found:
    print("找到可读数据的波特率:")
    for baud, n, txt in found:
        print("  %-8d %r" % (baud, txt[:100]))
else:
    print("所有波特率都没收到可读数据。")
    print()
    print("这基本可以确定：")
    print("  1) ESP32 的 TX(GPIO43) → TTL 的 RX 这根线没通")
    print("  2) 或者 ESP32 根本没运行（固件挂了/没上电）")
    print()
    print("判别方法：看 ESP32 板子 LED 是否亮，亮=有电，那问题在线。")
print("=" * 70)
