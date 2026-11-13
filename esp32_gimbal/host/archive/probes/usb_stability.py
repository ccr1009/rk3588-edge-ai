#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USB 串口稳定性测试：能连续工作多久不出错。

为什么要测"稳定性"而不只是"能不能通"：
    当前现象是 usb 1-1.1: failed to send control message: -32
    -32 = EPIPE，USB 端点失效。特征是"插上能用一阵，然后断"。
    这种间歇性故障不能靠单次测试判断，必须连续跑一段时间看会不会掉。

用法：
    python3 usb_stability.py          # 测 60 秒
    python3 usb_stability.py 180      # 测 3 分钟
"""
import sys
import time

import serial

PORT = "/dev/ttyUSB0"
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

print("=" * 68)
print("USB 串口稳定性测试  %s  共 %.0f 秒" % (PORT, DURATION))
print("=" * 68)

try:
    s = serial.Serial(PORT, 115200, timeout=0.3)
except Exception as e:
    print("❌ 打不开: %s" % e)
    print()
    print("这说明 USB 链路已经断了。先重插 USB 再看。")
    sys.exit(1)

print("✅ 已打开")
time.sleep(0.3)
s.reset_input_buffer()

t0 = time.time()
sent = 0
rx = 0
errors = 0
err_detail = ""

while time.time() - t0 < DURATION:
    try:
        s.write(b"Q\n")
        s.flush()
        sent += 1
        d = s.read(512)
        if d:
            rx += len(d)
    except Exception as e:
        errors += 1
        err_detail = str(e)
        if errors <= 3:
            print("  [%5.1fs] 错误: %s" % (time.time() - t0, e))
        # 尝试重开
        try:
            s.close()
        except Exception:
            pass
        time.sleep(0.5)
        try:
            s = serial.Serial(PORT, 115200, timeout=0.3)
            print("  [%5.1fs] 重开成功" % (time.time() - t0))
        except Exception as e2:
            print("  [%5.1fs] 重开失败: %s" % (time.time() - t0, e2))
            break
    time.sleep(0.1)

elapsed = time.time() - t0
try:
    s.close()
except Exception:
    pass

print()
print("=" * 68)
print("运行 %.1f 秒" % elapsed)
print("  发送 %d 条" % sent)
print("  收到 %d 字节" % rx)
print("  错误 %d 次" % errors)
if err_detail:
    print("  最后错误: %s" % err_detail)
print()

if errors == 0 and rx > 0:
    print("✅ 稳定：全程零错误，且有正常回包")
elif errors == 0 and rx == 0:
    print("⚠️  稳定但无回包：USB 没掉，但 ESP32 不响应")
    print("   → 检查 TTL 的 TX 有没有接到 ESP32 的 RX")
else:
    print("❌ 不稳定：%d 次错误 —— USB 链路在掉" % errors)
    print("   → 典型原因：舵机大电流通过 USB 地线回流（接地回路）")
print("=" * 68)
