#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持续监听串口原始字节，用来抓"热插拔瞬间"的输出。

用法：
    python3 watch_serial.py            # 监听 30 秒
    python3 watch_serial.py 60         # 监听 60 秒

为什么要有这个：
    ESP32 上电/复位时会打印 BOOT 横幅。如果能看到它，
    说明"固件活着 + 串口接收方向（ESP32→TTL）通"；
    如果热插拔后完全静默，说明要么固件没跑，要么 ESP32 TX → TTL RX 这根线断了。
    这是区分"固件问题"和"接线问题"最快的一招。
"""
import sys
import time

import serial

PORT = "/dev/ttyUSB0"
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

print("监听 %s 共 %.0f 秒 ..." % (PORT, DURATION))
print("请在过程中：把 ESP32 的 RST 键按一下，或拔插一次 ESP32 的 Type-C")
print()

s = serial.Serial(PORT, 115200, timeout=0.2)
s.reset_input_buffer()

t0 = time.time()
total = 0
chunks = []
while time.time() - t0 < DURATION:
    d = s.read(256)
    if d:
        total += len(d)
        chunks.append((time.time() - t0, d))
        try:
            txt = d.decode("utf-8", "replace")
        except Exception:
            txt = repr(d)
        print("  [%5.1fs] %s" % (time.time() - t0, txt.rstrip()))

s.close()

print()
print("=" * 60)
print("共收到 %d 字节" % total)
if total == 0:
    print("完全静默。可能原因（按概率）：")
    print("  1) 固件没在跑（ESP32 供电有电，但程序挂了/没烧进去）")
    print("  2) ESP32 的 TX(GPIO43) → TTL 的 RX 这根线没通或接反")
    print("  3) 波特率不对（固件是 115200）")
    print()
    print("下一步：把 TTL 的 TX/RX 两根线对调，再看一次。")
else:
    print("收到了数据 —— 串口接收方向是通的。")
    print("如果看到 'BOOT gimbal v2' 说明固件正常，问题在发送方向。")
