#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用固件的 V 命令探测 GPIO14 上有没有负载（判断信号线是否真接上）

原理：
    固件把引脚设成输入 + 上拉，读一次；再设成输入 + 下拉，读一次。
      · 两次读到不同值 → 引脚被外部电路"拉"着 = 有负载 = 接线通了
      · 两次都一样     → 悬空 = 没接东西
    这不需要万用表，是 MCU 自己做的测量。
"""
import socket
import sys
import time

s = socket.create_connection(("127.0.0.1", 9100), timeout=3)
f = s.makefile("rwb", buffering=0)
s.settimeout(0.8)


def send(cmd, wait=0.8):
    f.write((cmd + "\n").encode())
    time.sleep(wait)
    out = []
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            line = f.readline()
            if line:
                out.append(line.decode("utf-8", "replace").strip())
        except Exception:
            break
    return out


print("=" * 66)
print("引脚负载探测（判断舵机信号线是否真的接到 GPIO14）")
print("=" * 66)
print()

print("[1] V 14 —— 探测 GPIO14")
r = send("V 14", 1.5)
for x in r:
    print("    %s" % x)
print()

print("[2] V 15 —— 对照：GPIO15 应该是空的（tilt 没接）")
r2 = send("V 15", 1.5)
for x in r2:
    print("    %s" % x)
print()

print("[3] I —— 当前引脚配置")
r3 = send("I", 0.8)
for x in r3:
    print("    %s" % x)
print()

# 分析
def parse_v(lines):
    """从 V 输出里提取 pu/pd 状态"""
    for l in lines:
        if "VPIN" in l or "pu=" in l:
            return l
    return None

v14 = parse_v(r)
v15 = parse_v(r2)

print("=" * 66)
print("判据：")
print("  输出形如 VPIN 14 pu=1 pd=0 FLOATING")
print("    FLOATING = 引脚悬空，没接东西")
print("    LOADED / 有差异 = 接了外部电路")
print()
if v14:
    print("  GPIO14: %s" % v14)
    if "FLOATING" in v14.upper():
        print("  → ❌ 悬空！舵机信号线没接到 GPIO14")
    else:
        print("  → ✅ 有负载，信号线接着")
else:
    print("  ⚠️  没解析到 V 的输出，全部内容如上")
if v15:
    print("  GPIO15: %s  （对照：应该是 FLOATING）" % v15)
print("=" * 66)

s.close()
