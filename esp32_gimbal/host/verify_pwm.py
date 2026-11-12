#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 GPIO14 PWM 波形 —— 单独监听异步推送的 LB 统计行"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 9100

s = socket.create_connection((HOST, PORT), timeout=3)
f = s.makefile("rwb", buffering=0)
s.settimeout(0.5)


def send_only(cmd, wait=0.5):
    """只发命令，不读（读交给别的循环）"""
    f.write((cmd + "\n").encode())
    time.sleep(wait)


def drain(dur=1.0):
    """读所有可用行"""
    out = []
    t0 = time.time()
    while time.time() - t0 < dur:
        try:
            line = f.readline()
            if line:
                out.append(line.decode("utf-8", "replace").strip())
        except Exception:
            time.sleep(0.05)
    return out


print("=" * 66)
print("GPIO14 PWM 波形验证")
print("=" * 66)
print()

# 1. 中位
print("[1] P 0 0 （中位，期望 pw_avg≈1500us）")
send_only("P 0 0", 0.8)
drain(0.3)

# 2. 启动自测
print("[2] R 14 启动回环自测")
send_only("R 14", 0.5)
ack = drain(0.5)
print("    ACK: %r" % ack)

# 3. 收集统计（固件每秒推一次）
print("[3] 收集 4 秒统计 ...")
lines = drain(4.0)
lb = [l for l in lines if "LB" in l or "edges" in l]
if lb:
    for l in lb[-6:]:
        print("    %s" % l)
else:
    print("    没收到 LB 行，全部输出: %r" % lines[-8:])
print()

# 4. 满量程
print("[4] P 45 0 （期望 pw_avg≈2000us）")
send_only("P 45 0", 1.0)
lines2 = drain(3.5)
lb2 = [l for l in lines2 if "LB" in l or "edges" in l]
if lb2:
    for l in lb2[-4:]:
        print("    %s" % l)
else:
    print("    没收到 LB 行")
print()

# 5. 停止
print("[5] R -1 停止，H 回中")
send_only("R -1", 0.4)
drain(0.4)
send_only("H", 0.8)
r = drain(0.6)
print("    %r" % r)

s.close()

print()
print("=" * 66)
print("判据：")
print("  中位 pw_avg≈1500-1502，P45 时 ±2000，per_avg≈20000")
print("    → PWM 输出正常，舵机不动就是供电/接线问题")
print("  edges=0 NO_SIGNAL  → GPIO14 没有波形")
print("=" * 66)
