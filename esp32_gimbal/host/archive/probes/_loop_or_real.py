#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判断收到的数据是"ESP32 回的"还是"自己发的回环"。

方法：
    发一个 ESP32 绝对不会回的东西（比如带随机标记的字符串）。
    · 如果收到的内容 == 发送的内容 → 是回环（TX 和 RX 短接了）
    · 如果收到的是 S/OK/ERR 等协议响应 → ESP32 真的收到了
    · 如果收不到 → TX 没通
"""
import sys
import time
import uuid

import serial

PORT = "/dev/ttyUSB0"

s = serial.Serial(PORT, 115200, timeout=0.3)
time.sleep(0.5)
s.reset_input_buffer()

print("=" * 66)
print("区分「回环」和「真响应」")
print("=" * 66)
print()

# 测试1：发随机字符串（ESP32 不认识，如果收到原样内容就是回环）
marker = "MARKER_" + uuid.uuid4().hex[:8].upper()
print("[1] 发送随机标记: %s" % marker)
s.write((marker + "\n").encode())
s.flush()
time.sleep(0.8)
d = s.read(500)
print("    收到: %r" % d)
if marker.encode() in d:
    print("    ⚠️  收到了自己发的内容 → TX 和 RX 短接了（回环）")
else:
    print("    ✓ 没收到自己的内容（正常）")

print()

# 测试2：发 Q，看回什么
print("[2] 发送 'Q'（ESP32 应该回 'S 0.0 0.0 1'）")
s.reset_input_buffer()
s.write(b"Q\n")
s.flush()
time.sleep(1.0)
d = s.read(500)
print("    收到: %r" % d)
if b"S " in d:
    print("    ✅ ESP32 真的响应了！")
elif b"Q" == d.strip() or d.strip() == b"Q":
    print("    ⚠️  又收到了自己发的 Q → 确认是回环")
elif len(d) == 0:
    print("    ❌ 没有任何回复 → TX 方向不通")
else:
    print("    收到其他内容: %r" % d)

print()

# 测试3：发非法命令，ESP32 应回 ERR
print("[3] 发送 'ZZZZ'（ESP32 应回 'ERR 9 unknown_cmd'）")
s.reset_input_buffer()
s.write(b"ZZZZ\n")
s.flush()
time.sleep(1.0)
d = s.read(500)
print("    收到: %r" % d)
if b"ERR" in d:
    print("    ✅ ESP32 收到了！")
elif b"ZZZZ" in d:
    print("    ⚠️  收到自己发的 ZZZZ → 回环确认")
elif len(d) == 0:
    print("    ❌ 无回复")

s.close()
print()
print("=" * 66)
