#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""烧录后验证：确认固件版本 + GPIO14 波形 + 舵机响应

按顺序做三件事：
  1. 探测固件支持哪些命令（判断是不是 v2）
  2. R 14 回环自测（GPIO14 上有没有 50Hz 波形）
  3. 摆动测试（看舵机动不动）
"""
import socket
import sys
import time

s = socket.create_connection(("127.0.0.1", 9100), timeout=3)
f = s.makefile("rwb", buffering=0)
s.settimeout(1.0)


def send(cmd, wait=0.8):
    f.write((cmd + "\n").encode())
    time.sleep(wait)
    try:
        return f.readline().decode("utf-8", "replace").strip()
    except Exception:
        return "(无)"


def drain(dur=1.0):
    out = []
    t0 = time.time()
    while time.time() - t0 < dur:
        try:
            line = f.readline()
            if line:
                out.append(line.decode("utf-8", "replace").strip())
        except Exception:
            time.sleep(0.03)
    return out


print("=" * 68)
print("烧录后验证")
print("=" * 68)
print()

# ---------- 1. 固件版本 ----------
print("[1] 固件命令探测（v2 应该支持 I/R/V/G/T 等）")
supported = []
for cmd in ["Q", "I", "P 0 0", "S 100", "L 0", "G 14", "T 15", "V 14"]:
    r = send(cmd, 0.7)
    is_unknown = "unknown_cmd" in r
    mark = "✗" if is_unknown else "✓"
    print("    %s %-8s -> %r" % (mark, cmd, r))
    if not is_unknown and r:
        supported.append(cmd)
print()
print("    支持的命令: %s" % supported)
if "I" in supported:
    print("    ✅ 看起来是 v2 固件")
else:
    print("    ⚠️  仍不支持 I 命令，可能不是 v2")
print()

# ---------- 2. 波形自测 ----------
if "R 14" or True:
    print("[2] GPIO14 回环自测（R 14）")
    f.write(b"R 14\n")
    time.sleep(0.4)
    lines = drain(3.5)
    lb = [l for l in lines if "LB" in l or "edges" in l]
    if lb:
        for l in lb[-5:]:
            print("    %s" % l)
        joined = " ".join(lb)
        if "edges=0" in joined or "NO_SIGNAL" in joined:
            print()
            print("    ❌ GPIO14 没有波形！PWM 没输出")
        elif "pw_avg" in joined:
            print()
            print("    ✅ 有波形（看 pw_avg 是否 ≈1500us）")
    else:
        print("    (没收到 LB 行 —— 固件可能仍不支持 R 命令)")
        print("    收到的: %r" % lines[-4:])
    f.write(b"R -1\n")
    time.sleep(0.3)
    print()

# ---------- 3. 舵机摆动 ----------
print("[3] 摆动测试（请盯着舵机）")
print("    H -> %r" % send("H", 1.2))
for a in [-60, 0, 60, 0]:
    print("    P %+4d -> %r" % (a, send("P %d 0" % a, 1.3)))
print("    H -> %r" % send("H", 1.0))
print()
print("=" * 68)
print("请回答：舵机动了吗？")
print("=" * 68)

s.close()
