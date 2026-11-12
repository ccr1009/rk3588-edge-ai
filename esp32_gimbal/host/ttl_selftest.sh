#!/bin/bash
# TTL 自身收发通路验证
# 需要：把 TTL 的 TX 和 RX 用杜邦线短接（不要接 ESP32）
#
# 目的：排除"是不是 TTL 模块自己的通路坏了"
#   能收到 → TTL 通路好，问题在 ESP32 那端
#   收不到 → TTL 模块坏了

echo "=============================================================="
echo "TTL 模块自身收发通路测试"
echo "=============================================================="
echo
echo "前提：TTL 的 TX 和 RX 已经短接（一根杜邦线把这两个针连起来）"
echo "      ESP32 的线暂时全部拔掉"
echo

/root/rknn_env/bin/python - << 'PYEOF'
import serial
import time

s = serial.Serial("/dev/ttyUSB0", 115200, timeout=0.5)
time.sleep(0.3)
s.reset_input_buffer()

payload = b"TTL_LOOPBACK_CHECK_98765\n"
print("发送: %r" % payload)
s.write(payload)
s.flush()
time.sleep(0.5)
got = s.read(300)
print("收到: %r" % got)
print()

if payload in got or len(got) > 0:
    print("✅ TTL 模块收发通路正常")
    print("   结论：问题在 ESP32 那端的接线或 ESP32 的 TX 引脚")
    print()
    print("   下一步：")
    print("   1) 拔掉短接线")
    print("   2) TTL 的 RX → ESP32 标 TX 的针（用一根新的杜邦线）")
    print("   3) TTL 的 TX → ESP32 标 RX 的针")
    print("   4) TTL 的 GND → ESP32 的 GND")
    print("   5) 按 ESP32 的 RST")
else:
    print("❌ TTL 模块收发通路不通")
    print("   要么短接线没插好，要么模块的 RX 坏了")
    print("   先确认短接线插紧，再试一次")

s.close()
PYEOF
