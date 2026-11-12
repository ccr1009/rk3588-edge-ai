#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""串口本地回环测试 —— 判断 TTL 模块本身是否正常。

原理：
    把 TTL 模块的 TX 和 RX 用一根杜邦线短接，模块发出去的数据会立刻被自己收到。
    这就是经典的 "loopback test"，不需要任何外部设备。

    能收到 → 模块是好的，问题在"TTL 到 ESP32"那段接线
    收不到 → 模块坏了 / 串口驱动有问题 / 线本身断了

用法：
    # 1) 先短接 TX-RX
    python3 loopback_test.py
    # 2) 测完记得把短接线拔掉，否则 ESP32 的 TX 会和 TTL 的 TX 互顶
"""
import sys
import time

import serial

PORT = "/dev/ttyUSB0"

s = serial.Serial(PORT, 115200, timeout=0.5)
time.sleep(0.3)
s.reset_input_buffer()

print("=" * 64)
print("TTL 模块回环测试")
print("=" * 64)
print()
print("前提：TTL 的 TX 和 RX 已经用杜邦线短接")
print()

test_bytes = b"LOOPBACK_TEST_12345\n"
print("[1] 发送 %r" % test_bytes)
s.write(test_bytes)
s.flush()

time.sleep(0.5)
got = s.read(500)
print("    收到 %r" % got)
print()

ok = test_bytes in got or len(got) > 0
if ok:
    print("✅ 回环成功 —— TTL 模块工作正常")
    print("   结论：问题在 TTL ↔ ESP32 的接线，不是模块本身")
    print()
    print("   下一步：")
    print("   1) 拔掉短接线")
    print("   2) TTL 的 RX → ESP32 板子标 TX 的针")
    print("   3) TTL 的 TX → ESP32 板子标 RX 的针")
    print("   4) TTL 的 GND → ESP32 GND")
    print("   5) 按 ESP32 的 RST 键")
else:
    print("❌ 回环失败 —— 问题不在 ESP32 那头")
    print()
    print("   可能：")
    print("   1) 短接线没插好（TX/RX 没真正短接）")
    print("   2) TTL 模块坏了")
    print("   3) 杜邦线断了")
    print()
    print("   下一步：换一根杜邦线再试")

s.close()
print()
print("=" * 64)
