#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""供电/信号排查（无需交互）"""
import socket
import time

s = socket.create_connection(("127.0.0.1", 9100), timeout=3)
f = s.makefile("rwb", buffering=0)
s.settimeout(1.0)


def send(cmd, wait=1.0):
    f.write((cmd + "\n").encode())
    time.sleep(wait)
    try:
        return f.readline().decode("utf-8", "replace").strip()
    except Exception:
        return "(无)"


print("=" * 66)
print("供电排查测试 —— 请盯着舵机看/听")
print("=" * 66)
print()

print("[1] 回中，然后停顿 2 秒")
print("    H ->", send("H", 1.2))
time.sleep(2.0)
print("    此时摇臂应该有保持力矩")
print()

print("[2] 极端摆动：-90 → +90，每步 2 秒（足够看清）")
for a in [-90, -60, -30, 0, 30, 60, 90, 0]:
    r = send("P %d 0" % a, 1.8)
    print("    P %+4d -> %-16s  <-- 摇臂应该动了" % (a, r))
print()

print("[3] 快速连续摆动（如果舵机有电会明显抖动）")
for _ in range(6):
    send("P 60 0", 0.35)
    send("P -60 0", 0.35)
print("    快速摆动完成")
print()

print("[4] 回中")
print("    H ->", send("H", 1.2))
print()
print("=" * 66)
print("请回答：")
print("  1. 摇臂有阻力吗（拨得动吗）？")
print("  2. 摆动时舵机有任何动作/声音吗？")
print("  3. 快速摆动时有抖动吗？")
print("=" * 66)

s.close()
