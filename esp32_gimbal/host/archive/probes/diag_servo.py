#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""舵机诊断：先确认串口通不通，再逐层往下查"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
import serial

PORT = "/dev/ttyUSB0"

print("=" * 64)
print("ESP32 云台诊断")
print("=" * 64)

try:
    s = serial.Serial(PORT, 115200, timeout=1.0)
except Exception as e:
    print("串口打不开: %s" % e)
    sys.exit(1)

time.sleep(0.5)
s.reset_input_buffer()


def send(cmd, wait=1.0):
    s.write((cmd + "\n").encode())
    s.flush()
    time.sleep(wait)
    return s.read(400).decode(errors="replace").strip()


print()
print("[1] 固件活着吗")
r = send("Q")
print("    Q -> %r" % r)
if not r:
    print("    ❌ 没有任何回复 —— 固件可能没跑 / 串口线序错 / ESP32 没电")
    print("    查：ESP32 的 Type-C 有没有接电源？USB-TTL 的 TX/RX 是不是交叉接？")
    s.close()
    sys.exit(1)
print("    ✅ 固件响应正常")

print()
print("[2] 引脚配置")
r = send("I")
print("    I -> %r" % r)
print("    （期望 GPIN 14 15）")

print()
print("[3] 小角度摆动 —— 眼睛看两颗舵机")
for cmd in ["P -20 0", "P 0 20", "P 20 -20", "P 0 0"]:
    r = send(cmd, 1.2)
    print("    %-12s -> %s" % (cmd, r))

print()
print("[4] 回环自测：Pan 脚(GPIO14)有没有波形")
print("    发 R 14，等 2 秒看统计")
send("R 14", 0.5)
time.sleep(2.5)
r = s.read(800).decode(errors="replace").strip()
print("    %r" % r)
if "edges=0" in r or "NO_SIGNAL" in r:
    print("    ❌ GPIO14 没有边沿 —— 固件没输出 PWM")
elif "pw_avg" in r:
    print("    ✅ 有 PWM 波形")
send("R -1", 0.3)

print()
print("[5] 回环自测：Tilt 脚(GPIO15)有没有波形")
send("R 15", 0.5)
time.sleep(2.5)
r = s.read(800).decode(errors="replace").strip()
print("    %r" % r)
if "edges=0" in r or "NO_SIGNAL" in r:
    print("    ❌ GPIO15 没有边沿")
elif "pw_avg" in r:
    print("    ✅ 有 PWM 波形")
send("R -1", 0.3)

print()
print("[6] 引脚负载探测：判断舵机信号线有没有真的接上")
r = send("V 14 15", 1.5)
print("    V 14 15 -> %r" % r)
print("    （FLOATING=悬空没接；PU/PD=有负载，说明舵机信号线接着）")

print()
print("[7] 回中")
print("    H ->", send("H", 1.0))

s.close()
print()
print("=" * 64)
