#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判断 TTL→ESP32 方向到底通不通。

思路：
    固件对任何收到的行都会回应（即使是不认识的命令也会回 ERR 9 unknown_cmd）。
    所以只要 ESP32 收到了我们的字节，就一定有输出。
    如果连 ERR 都没有，说明字节根本没进 ESP32。

    但有个坑：ESP32 的 UART0 RX 是 GPIO44，如果 TTL 的 TX 没接上，
    我们发出去的字节就掉在地上，什么都不会发生。

额外验证：
    给 ESP32 发一个**足够多的字节**，让它缓冲区里留下痕迹；
    然后立刻读，看有没有任何回应。
"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
import serial

s = serial.Serial("/dev/ttyUSB0", 115200, timeout=0.5)
time.sleep(0.5)
s.reset_input_buffer()

print("=" * 64)
print("TTL -> ESP32 方向测试")
print("=" * 64)

# 1. 先确认接收方向还通（发个复位看不见，但可以发个命令看有没有反应）
print()
print("[1] 先确认还能收到数据（读 1 秒看有无主动输出）")
t0 = time.time()
buf = b""
while time.time() - t0 < 1.0:
    d = s.read(100)
    if d:
        buf += d
print("    收到 %d 字节" % len(buf))

# 2. 连发大量字节，确保不是"偶尔漏"
print()
print("[2] 连发 100 次 'Q'（每次带换行）")
for i in range(100):
    s.write(b"Q\n")
    s.flush()
    time.sleep(0.02)
time.sleep(1.0)
d = s.read(2000)
print("    收到 %d 字节: %r" % (len(d), d[:200]))

# 3. 试不同的命令格式
print()
print("[3] 试不同格式（可能有的是 CRLF 有的是 LF）")
s.reset_input_buffer()
for payload in [b"Q\r\n", b"Q\n", b"Q\r", b"\r\nQ\n", b" H \n"]:
    s.write(payload)
    s.flush()
    time.sleep(0.6)
    d = s.read(300)
    print("    %-12r -> %r" % (payload, d))

# 4. 关键：发一个非法命令，固件应该回 ERR 9 unknown_cmd
print()
print("[4] 非法命令测试（固件应回 ERR 9 unknown_cmd）")
s.reset_input_buffer()
s.write(b"ZZZZ\n")
s.flush()
time.sleep(1.0)
d = s.read(300)
print("    ZZZZ -> %r" % d)
if d:
    print("    ✅ 说明 ESP32 收到了我们的字节！")
else:
    print("    ❌ 说明字节没进 ESP32 —— TTL 的 TX 没接到 ESP32 的 RX(GPIO44)")

s.close()
print()
print("=" * 64)
