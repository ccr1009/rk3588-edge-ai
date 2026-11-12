#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台持续监听：60 秒内记录所有收到的字节，任何非零输入都打印。

不需要交互，可以在 SSH 里直接跑。
你只要在这 60 秒内按一下 ESP32 的 RST 即可。
"""
import time

import serial

PORT = "/dev/ttyUSB0"
DURATION = 60

print("=" * 70)
print("持续监听 %d 秒 —— 请按一下 ESP32 的 RST" % DURATION)
print("=" * 70)
print()

s = serial.Serial(PORT, 115200, timeout=0.1)
s.reset_input_buffer()

t0 = time.time()
total = 0
text_blocks = 0
noise_blocks = 0
samples = []

while time.time() - t0 < DURATION:
    d = s.read(512)
    if not d:
        continue
    total += len(d)
    p = sum(1 for b in d if 32 <= b < 127 or b in (9, 10, 13))
    ratio = p / len(d)
    elapsed = time.time() - t0

    if ratio > 0.6:
        text_blocks += 1
        txt = d.decode("utf-8", "replace").rstrip()
        print("  [%5.1fs] 文本: %s" % (elapsed, txt[:200]))
        samples.append(txt)
    else:
        noise_blocks += 1
        if noise_blocks <= 5:
            print("  [%5.1fs] 噪声(%d字节): %r" % (elapsed, len(d), d[:48]))

    # 每 10 秒报进度
    if int(elapsed) % 10 == 0 and int(elapsed) > 0:
        pass

s.close()

print()
print("=" * 70)
print("总共 %d 字节  |  文本块 %d  |  噪声块 %d" % (total, text_blocks, noise_blocks))
print()
if total == 0:
    print("❌ 完全没有数据 —— ESP32 的 TX 没有任何输出")
    print()
    print("排查顺序：")
    print("  1. ESP32 的 LED 亮吗？（不亮=没电）")
    print("  2. TTL 的 RX 线是否插在 ESP32 标 TX 的针上？（这是接收方向）")
    print("  3. 换一根杜邦线试")
    print("  4. 用万用表量 ESP32 TX 针对 GND 的电压：待机应该接近 3.3V")
elif text_blocks > 0:
    print("✅ 收到可读文本！")
    print("最后一条: %r" % samples[-1][:150])
else:
    print("⚠️  只有噪声，没有文本")
    print("   可能是波特率不对，或者信号质量差")
print("=" * 70)
