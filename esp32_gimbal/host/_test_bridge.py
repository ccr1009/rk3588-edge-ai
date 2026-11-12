#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试桥接服务：通过 TCP 9100 发命令，看 ESP32 是否响应"""
import socket
import sys
import time

HOST = "127.0.0.1"
PORT = 9100

print("=" * 64)
print("云台桥接服务测试")
print("=" * 64)

try:
    s = socket.create_connection((HOST, PORT), timeout=3)
except OSError as e:
    print("连不上桥接服务: %s" % e)
    sys.exit(1)

f = s.makefile("rwb", buffering=0)


def send(cmd, wait=0.6):
    f.write((cmd + "\n").encode())
    time.sleep(wait)
    try:
        s.settimeout(0.5)
        line = f.readline()
        return line.decode("utf-8", "replace").strip()
    except Exception:
        return "(超时)"


print()
for cmd in ["Q", "H", "P 0 0", "P 10 0", "P -10 0", "P 0 0", "STAT"]:
    r = send(cmd)
    print("  %-10s -> %r" % (cmd, r))

s.close()
print()
print("✅ 测试完成")
