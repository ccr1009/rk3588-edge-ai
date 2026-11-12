#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双向收发测试：确认 TX 方向（板子→TTL）和 RX 方向（TTL→板子）

刚才 watch_serial 收到了启动日志（板子→TTL 通），
但发 Q 没回复（TTL→板子 可能不通）。
这里把两个方向分开验证。
"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
import serial

s = serial.Serial("/dev/ttyUSB0", 115200, timeout=0.5)
time.sleep(0.5)

print("=" * 64)
print("双向测试")
print("=" * 64)

# 先清空
s.reset_input_buffer()
time.sleep(0.3)
s.reset_input_buffer()

print()
print("[A] 发送方向：发 5 次 Q，每次读完")
for i in range(5):
    s.write(b"Q\n")
    s.flush()
    time.sleep(0.8)
    d = s.read(300)
    print("  第%d次 -> %r" % (i + 1, d))

print()
print("[B] 换个思路：先发个 H（回中），再连发多次")
for cmd in [b"H\n", b"H\n", b"Q\n"]:
    s.write(cmd)
    s.flush()
    time.sleep(1.0)
    d = s.read(300)
    print("  %r -> %r" % (cmd.strip(), d))

print()
print("[C] 检查是否 DTR/RTS 影响了 ESP32 的复位")
print("  当前 dtr=%s rts=%s" % (s.dtr, s.rts))
s.dtr = False
s.rts = False
time.sleep(0.5)
s.reset_input_buffer()
s.write(b"Q\n")
s.flush()
time.sleep(1.5)
d = s.read(400)
print("  设置 dtr/rts=False 后 -> %r" % d)

print()
print("[D] 边发边读（不等待）")
s.reset_input_buffer()
s.write(b"Q\n")
s.flush()
buf = b""
t0 = time.time()
while time.time() - t0 < 2.0:
    chunk = s.read(100)
    if chunk:
        buf += chunk
print("  2 秒内收到 %d 字节: %r" % (len(buf), buf))

s.close()
print()
print("=" * 64)
