#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""舵机实动测试：通过桥接服务发角度，让舵机真的摆动。

回答两个问题：
  1. 舵机动了吗（需要你眼睛看）
  2. 串口回复是否正常（我能看到）
"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 9100

print("=" * 64)
print("舵机实动测试 —— 请盯着舵机摇臂看")
print("=" * 64)
print()

try:
    s = socket.create_connection((HOST, PORT), timeout=3)
except OSError as e:
    print("连不上桥接: %s" % e)
    sys.exit(1)

f = s.makefile("rwb", buffering=0)
s.settimeout(1.0)


def send(cmd, wait=0.8):
    f.write((cmd + "\n").encode())
    time.sleep(wait)
    try:
        return f.readline().decode("utf-8", "replace").strip()
    except Exception:
        return "(超时)"


print("1) 回中")
print("   H ->", send("H", 1.0))
print()

print("2) 大幅摆动（每个位置停 1.2 秒，应该看到明显转动）")
angles = [-40, -20, 0, 20, 40, 20, 0, -20, -40, 0]
for a in angles:
    r = send("P %d 0" % a, 1.2)
    print("   P %+4d  -> %r" % (a, r))
print()

print("3) 回中")
print("   H ->", send("H", 1.0))
print()
print("4) 查询")
print("   Q ->", send("Q", 0.6))

s.close()
print()
print("=" * 64)
print("刚才舵机动了吗？")
print("  动了      -> 闭环可用，可以跑追踪")
print("  没动      -> 查供电/共地/PWM 线")
print("  只在抖    -> 可能是 PWM 频率或供电不稳")
print("=" * 64)
