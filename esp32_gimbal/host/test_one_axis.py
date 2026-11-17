#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_one_axis.py — 一次只测一颗舵机（倒计时提示，不需要按回车）

为什么不用 input()
------------------
2026-11-16 踩到：把这个脚本用 `python - <<'PY' ... PY` 喂进去时，
heredoc 已经把 stdin 占满了，`input()` 立刻读到 EOF →
`EOFError`，测试一行都没跑到。改成倒计时后不依赖 stdin。

跑法（必须先停桥接，它独占 USB）：
    bash /root/esp32_gimbal/stop_bridge.sh
    /root/rknn_env/bin/python /root/esp32_gimbal/test_one_axis.py top
    /root/rknn_env/bin/python /root/esp32_gimbal/test_one_axis.py bottom

参数：
    top     只驱动 PAN 通道（GPIO14）= 你接的顶部俯仰
    bottom  只驱动 TILT 通道（GPIO15）= 你接的底部水平

★ 绝对不要发 'X'：固件里 X = start_scan()，每 1.2s 换一个引脚，
   扫描期间 P 命令完全不生效（2026-11-06 因此把整轮测试做废）。
"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial


def one(ser, cmd, wait=0.8):
    ser.write((cmd + "\n").encode())
    ser.flush()
    t0 = time.time()
    while time.time() - t0 < wait:
        line = ser.readline()
        if line:
            v = line.decode("utf-8", "replace").strip()
            if v:
                return v
    return None


def countdown(n, note):
    for i in range(n, 0, -1):
        sys.stdout.write("\r      倒数 %d ...  %s          " % (i, note))
        sys.stdout.flush()
        time.sleep(1.0)
    sys.stdout.write("\r" + " " * 64 + "\r")
    sys.stdout.flush()


def main():
    axis = sys.argv[1] if len(sys.argv) > 1 else "top"
    if axis not in ("top", "bottom"):
        print("用法: test_one_axis.py {top|bottom}")
        return 2

    ser = UsbCdcSerial(timeout=0.4)
    ser.reset_input_buffer()

    # 清场：只发停止类命令，绝不发 X
    for c in ["D 0", "A"]:
        one(ser, c, 0.3)
    time.sleep(0.3)
    ser.reset_input_buffer()

    pins = one(ser, "I", 1.2)
    print("=" * 64)
    print(" 单轴测试：%s" % ("顶部俯仰（GPIO14 / PAN 通道）" if axis == "top"
                             else "底部水平（GPIO15 / TILT 通道）"))
    print("=" * 64)
    print("  固件引脚: %s        （期望 GPIN 14 15）" % pins)
    if not pins or not pins.startswith("GPIN"):
        print("  ❌ 通信异常，先跑 check_link.py")
        return 1
    if "GPIN 14 15" not in pins:
        print("  ⚠️ 引脚不是 14 15，可能误发过 X（扫描）。等 30s 或复位后再跑")
        return 1

    print()
    print("  ★ 只盯住【%s】那颗舵机，其它地方不该有动作"
          % ("顶部" if axis == "top" else "底部"))
    print("  预期动作：抬头/左转 → 回中 → 低头/右转 → 回中")
    countdown(3, "准备")

    if axis == "top":
        seq = [("P %d 0" % v, "P %+4d 0" % v) for v in (-35, 0, 35, 0)]
    else:
        seq = [("P 0 %d" % v, "P 0 %+4d" % v) for v in (-35, 0, 35, 0)]

    for cmd, label in seq:
        r = one(ser, cmd, 0.3)
        print("      %-12s -> %s" % (label, r))
        time.sleep(2.0)          # 留 2 秒看动作

    one(ser, "P 0 0", 0.4)
    time.sleep(0.5)
    ser.close()

    print()
    print("=" * 64)
    print(" 结论登记（人眼判断）:")
    print("   动了     → GPIO %s 通路正常，这颗舵机是好的"
          % ("14" if axis == "top" else "15"))
    print("   没动     → 查共地线 / DC 模块是否够电流 / 换根橙线")
    print("   动了但方向反 → 记下来，追踪时用 --no-invert-pan/tilt 调")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
