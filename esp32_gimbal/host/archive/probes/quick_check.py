#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性综合诊断：同时验证接收方向和发送方向。

设计思路：
    之前分开测有两个问题：
      1) 每次只测一个方向，用户要动好几次线
      2) "监听 45 秒等按 RST"依赖人的操作时机，容易错过

    这个脚本边听边说：
      · 全程持续读串口（捕获任何主动输出，包括复位横幅）
      · 每 0.5 秒发一次 Q（如果 ESP32 收到，会回 S 0.0 0.0 1）
      · 最后汇总两个方向的状态

用法：
    python3 quick_check.py            # 跑 20 秒
    python3 quick_check.py 40         # 跑 40 秒
"""
import sys
import time

import serial

PORT = "/dev/ttyUSB0"
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0

print("=" * 66)
print("综合诊断 %s  共 %.0f 秒" % (PORT, DURATION))
print("=" * 66)
print()
print("过程中请按一下 ESP32 的 RST 键（或拔插 ESP32 的 Type-C）")
print()

s = serial.Serial(PORT, 115200, timeout=0.3)
time.sleep(0.3)
s.reset_input_buffer()

rx_bytes = b""          # 收到的字节（ESP32 -> 我们）
sent_count = 0
t0 = time.time()
last_send = 0.0
last_report = 0.0

while time.time() - t0 < DURATION:
    now = time.time()

    # 每 0.5 秒发一次 Q
    if now - last_send > 0.5:
        try:
            s.write(b"Q\n")
            s.flush()
            sent_count += 1
        except Exception as e:
            print("  写失败: %s" % e)
        last_send = now

    # 持续读
    d = s.read(512)
    if d:
        rx_bytes += d
        txt = d.decode("utf-8", "replace")
        print("  [%5.1fs] <<< %s" % (now - t0, txt.rstrip()))

    # 每 5 秒报一次进度
    if now - last_report > 5.0:
        last_report = now
        print("  ---- %4.1fs: 已发 %d 条 Q，收到 %d 字节 ----"
              % (now - t0, sent_count, len(rx_bytes)))

s.close()

print()
print("=" * 66)
print("结果")
print("=" * 66)
print("  发送：%d 条 Q 指令" % sent_count)
print("  接收：%d 字节" % len(rx_bytes))
print()

# 判断
rx_ok = len(rx_bytes) > 0
# 如果收到了 "S " 或 "OK" 或 "ERR"，说明 ESP32 收到我们的命令了
tx_ok = (b"S " in rx_bytes) or (b"OK" in rx_bytes) or (b"ERR" in rx_bytes)
boot_seen = b"BOOT" in rx_bytes or b"ESP-ROM" in rx_bytes

print("  接收方向 ESP32→TTL : %s" % ("✅ 通" if rx_ok else "❌ 不通"))
print("  发送方向 TTL→ESP32 : %s" % ("✅ 通" if tx_ok else "❌ 不通"))
if boot_seen:
    print("  启动横幅            : ✅ 看到了（ESP32 确实复位过，固件在跑）")
print()

if rx_ok and tx_ok:
    print("🎉 双向都通！可以跑追踪了。")
elif rx_ok and not tx_ok:
    print("单向通：能收到 ESP32 的数据，但 ESP32 收不到我们的命令。")
    print("        → TTL 的 TX 没接到 ESP32 的 RX(GPIO44)")
    print("        → 检查这根线：换线 / 插紧 / 确认插在标 RX 的针上")
elif not rx_ok and tx_ok:
    print("单向通：ESP32 收到了命令但回不来（几乎不可能，检查逻辑）")
else:
    print("双向都不通。请按顺序检查：")
    print("  1) ESP32 有没有电（Type-C 插着？板载 LED 亮？）")
    print("  2) TTL 的 GND 有没有接到 ESP32 的 GND")
    print("  3) 两根信号线是不是交叉接（TTL TX→ESP32 RX，TTL RX→ESP32 TX）")
    print("  4) 换两根杜邦线试试（线断是最常见的）")
print("=" * 66)
