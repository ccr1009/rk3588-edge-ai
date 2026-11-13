#!/usr/bin/env python3
# 简单探测：串口能不能打开
import sys
try:
    import serial
except ImportError:
    print("no pyserial")
    sys.exit(1)

try:
    s = serial.Serial("/dev/ttyUSB0", 115200, timeout=0.5)
    print("OK 能打开")
    s.close()
except Exception as e:
    print("FAIL 打不开: %s" % e)
    sys.exit(1)
