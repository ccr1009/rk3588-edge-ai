#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""串口原始测试：只发 Q、读原始字节，不做任何解析"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
import serial

s = serial.Serial("/dev/ttyUSB0", 115200, timeout=1.0)
time.sleep(0.5)
s.reset_input_buffer()

print("=== 第1轮：发一次 Q，读 2 秒 ===")
s.write(b"Q\n")
s.flush()
time.sleep(2.0)
data = s.read(500)
print("收到 %d 字节: %r" % (len(data), data))

print()
print("=== 第2轮：连续发 20 次 Q，每次间隔 0.2s ===")
got = 0
for i in range(20):
    s.write(b"Q\n")
    s.flush()
    time.sleep(0.2)
    d = s.read(200)
    if d:
        print("  第 %d 次有回复: %r" % (i + 1, d))
        got += 1
        if got >= 3:
            break
if got == 0:
    print("  20 次全部无回复")

print()
print("=== 第3轮：读 3 秒看有没有主动上报（比如重启横幅）===")
s.reset_input_buffer()
t0 = time.time()
buf = b""
while time.time() - t0 < 3:
    d = s.read(100)
    if d:
        buf += d
print("自主输出 %d 字节: %r" % (len(buf), buf[:200]))

s.close()
print()
print("=== 结论 ===")
if len(buf) == 0 and got == 0:
    print("串口完全静默 —— 大概率是：")
    print("  1) ESP32 没上电（Type-C 供电断了？）")
    print("  2) USB-TTL 的 TX/RX 接反或没接")
    print("  3) 固件没跑起来（但那样通常会有 boot 日志）")
else:
    print("串口有数据，检查协议/波特率")
