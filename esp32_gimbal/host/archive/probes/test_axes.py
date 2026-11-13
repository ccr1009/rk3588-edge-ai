#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_axes.py — 分别测试 pan 轴和 tilt 轴，确认哪个舵机是好的、接在哪

背景（2026-11-06）：
    · 原 pan 舵机确认烧毁（5V✅ 共地✅ PWM✅ 但完全不动）
    · 把第二颗舵机换上去 → 用户反馈「tilt 动了」→ 说明这颗舵机是好的
    · 现在需要确认：这颗好舵机插在 GPIO14 还是 GPIO15？
      以及另一个轴是不是空的？

这个脚本把两个轴分开测，每个轴单独做一次明显的摆动，让人能对上号。

★ 注意：绝对不要发 'X' 命令！固件里 X = start_scan()，会每 1.2s 换一个引脚，
   期间 P 命令完全不生效（踩过一次，测试结果全废）。

跑法：
    bash /root/esp32_gimbal/stop_bridge.sh
    /root/rknn_env/bin/python /root/esp32_gimbal/test_axes.py
"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial


def drain(ser, dur):
    t0 = time.time()
    while time.time() - t0 < dur:
        ser.readline()


def one(ser, cmd, wait=0.6):
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


def say(t=""):
    print(t)
    sys.stdout.flush()


def cd(n, note=""):
    for i in range(n, 0, -1):
        sys.stdout.write("\r      倒数 %d ...  %s          " % (i, note))
        sys.stdout.flush()
        time.sleep(1.0)
    sys.stdout.write("\r" + " " * 62 + "\r")
    sys.stdout.flush()


def main():
    say("=" * 72)
    say("两轴分离测试 —— 确认哪个舵机是好的、接在哪个引脚")
    say("=" * 72)
    say()

    try:
        ser = UsbCdcSerial(timeout=0.4)
    except Exception as e:
        say("打不开 ESP32: %s" % e)
        say("先停桥： bash /root/esp32_gimbal/stop_bridge.sh")
        return 1

    ser.reset_input_buffer()

    # 前置：确认引脚和通信
    r = one(ser, "I", 1.2)
    say("固件: %s" % r)
    if not r or not r.startswith("GPIN"):
        say("通信异常，先解决这个")
        return 1
    if "GPIN 14" not in r:
        say()
        say("⚠️ 引脚不是 14！可能之前误发过 X（扫描）。等它跑完...")
        for _ in range(4):
            time.sleep(8)
            r = one(ser, "I", 1.2)
            say("   现在: %s" % r)
            if r and "GPIN 14" in r:
                break
        else:
            say("   还是没回到 14，先复位板子再跑")
            return 1

    # 清场：只发停止类命令，绝不发 X
    for c in ["D 0", "A"]:
        one(ser, c, 0.4)
    time.sleep(0.4)
    ser.reset_input_buffer()

    say()
    say("接线说明（现在的状态）：")
    say("  GPIO14 = PAN 轴   （第一颗，已确认烧毁）")
    say("  GPIO15 = TILT 轴  （第二颗，你刚说它动了）")
    say()

    # ---------------- PAN 轴 ----------------
    say("=" * 72)
    say("第 1 段：只动 PAN 轴（GPIO14）")
    say("=" * 72)
    say("  发 P -40 0 → P 40 0 → P 0 0")
    say("  ★ 看【底部水平旋转】那个舵机动不动")
    say()
    cd(4, "盯住底部那个舵机")

    for pan in (-40, 40, 0):
        one(ser, "P %d 0" % pan, 0.3)
        say("      P %+4d 0" % pan)
        drain(ser, 2.0)

    # ---------------- TILT 轴 ----------------
    say()
    say("=" * 72)
    say("第 2 段：只动 TILT 轴（GPIO15）")
    say("=" * 72)
    say("  发 P 0 -40 → P 0 40 → P 0 0")
    say("  ★ 看【上部俯仰】那个舵机动不动")
    say()
    cd(4, "盯住上部那个舵机")

    for tilt in (-40, 40, 0):
        one(ser, "P 0 %d" % tilt, 0.3)
        say("      P 0 %+4d" % tilt)
        drain(ser, 2.0)

    # ---------------- 双轴同时 ----------------
    say()
    say("=" * 72)
    say("第 3 段：两轴同时动（如果两个都接着，两个应该都在动）")
    say("=" * 72)
    say()
    cd(3, "两个都盯住")

    for pan, tilt in [(-30, -20), (30, 20), (-30, 20), (30, -20), (0, 0)]:
        one(ser, "P %d %d" % (pan, tilt), 0.3)
        say("      P %+4d %+4d" % (pan, tilt))
        drain(ser, 1.6)

    one(ser, "P 0 0", 0.4)
    drain(ser, 0.6)
    ser.close()

    say()
    say("=" * 72)
    say("请回答：")
    say("=" * 72)
    say()
    say("  第 1 段（PAN / GPIO14）：动 还是 不动？")
    say("  第 2 段（TILT / GPIO15）：动 还是 不动？")
    say("  第 3 段（两轴同时）：哪个在动？")
    say()
    say("  可能的组合：")
    say("   · PAN 动、TILT 不动 → 好舵机插在 GPIO14，GPIO15 空着或那颗也坏了")
    say("   · PAN 不动、TILT 动 → 好舵机插在 GPIO15（跟你说的对得上）")
    say("   · 两个都动           → 两颗舵机都在，pan 那颗也没事了？")
    say("   · 两个都不动         → 有问题，回来找我")
    say("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
