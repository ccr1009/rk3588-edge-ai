#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_gpio13.py — 测 GPIO13 上那颗底部舵机

当前接线（2026-11-06 23:21 用户确认）：
    底部舵机（pan）  → GPIO13   ← 未测试
    顶部舵机（tilt） → GPIO14   ← 已验证会动（servo_live.py 的 D 1 驱动的就是 GPIO14）

问题：固件默认 PAN=GPIO14、TILT=GPIO15。现在实际接线是 13 和 14，
     所以两条轴都没对上：
        · GPIO13 上那颗，固件根本没在驱动 → 不会动
        · GPIO14 上那颗，被当成 PAN 驱动 → 会动，但那颗是装在顶部的

这个脚本用固件的 G 命令把 PAN 通道切到 GPIO13，就能测那颗舵机。

★ 绝不发 X 命令（=引脚扫描，会让 P 命令失效）

跑法：
    bash /root/esp32_gimbal/stop_bridge.sh
    /root/rknn_env/bin/python /root/esp32_gimbal/test_gpio13.py
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


def drain(ser, dur):
    t0 = time.time()
    while time.time() - t0 < dur:
        ser.readline()


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
    say("测试 GPIO13 上的底部舵机")
    say("=" * 72)
    say()
    say("当前接线：底部 → GPIO13，顶部 → GPIO14")
    say("固件默认：PAN → GPIO14，TILT → GPIO15")
    say()
    say("★ 注意：GPIO14 上那颗会跟着动（它被当成 PAN），")
    say("   请只观察【底部】那个舵机（接在 GPIO13 上的）★")
    say()

    try:
        ser = UsbCdcSerial(timeout=0.4)
    except Exception as e:
        say("打不开 ESP32: %s" % e)
        say("先停桥： bash /root/esp32_gimbal/stop_bridge.sh")
        return 1

    ser.reset_input_buffer()
    r = one(ser, "I", 1.2)
    say("固件: %s" % r)
    if not r or not r.startswith("GPIN"):
        say("通信异常")
        return 1
    if "GPIN 14" not in r:
        say("⚠️ 引脚状态异常（可能之前扫描没跑完），等 30 秒...")
        time.sleep(30)
        r = one(ser, "I", 1.2)
        say("   现在: %s" % r)

    # 清场：只发停止类命令
    for c in ["D 0", "A"]:
        one(ser, c, 0.4)
    time.sleep(0.4)
    ser.reset_input_buffer()

    # ---------- 第 1 步：先确认 GPIO14 那颗（已知会动）作为对照 ----------
    say()
    say("=" * 72)
    say("对照测试：先让 GPIO14 动一下（确认整套系统还活着）")
    say("=" * 72)
    one(ser, "G 14", 0.8)
    r = one(ser, "I", 0.8)
    say("  G 14 后: %s（应该是 GPIN 14 15）" % r)
    say()
    say("  ★ 看顶部那个舵机，应该会动 ★")
    cd(3, "盯住顶部舵机")
    for pan in (-30, 30, 0):
        one(ser, "P %d 0" % pan, 0.3)
        drain(ser, 1.8)
    say("  对照结束。顶部舵机应该动过了。")

    # ---------- 第 2 步：把 PAN 通道切到 GPIO13 ----------
    say()
    say("=" * 72)
    say("关键测试：把 PAN 通道切到 GPIO13，测底部那颗舵机")
    say("=" * 72)

    r = one(ser, "G 13", 0.9)
    say("  G 13 回复: %s" % r)
    r = one(ser, "I", 0.9)
    say("  现在引脚: %s  ← 应该是 GPIN 13 15" % r)
    if r and "GPIN 13" not in r:
        say("  ⚠️ 引脚没切过去，测试结果不可信")

    say()
    say("  ★★ 现在请只盯【底部】那个舵机 ★★")
    say("  （顶部那颗不会动了，因为 GPIO14 不再被驱动）")
    say()
    cd(4, "盯住底部舵机")

    for pan in (-40, 40, -40, 40, 0):
        one(ser, "P %d 0" % pan, 0.3)
        say("      P %+4d 0" % pan)
        drain(ser, 1.8)

    # ---------- 第 3 步：恢复默认 ----------
    say()
    say("恢复默认引脚（PAN → GPIO14）")
    one(ser, "G 14", 0.8)
    one(ser, "P 0 0", 0.4)
    r = one(ser, "I", 0.9)
    say("  最终: %s" % r)

    ser.close()

    say()
    say("=" * 72)
    say("请回答：把 PAN 切到 GPIO13 之后（第 2 步），底部舵机动了吗？")
    say("=" * 72)
    say()
    say("  动了   → 底部那颗也是好的！两颗舵机都能用")
    say("          但需要改接线或改固件默认引脚（见下）")
    say("  没动   → 底部那颗确认烧了，买 MG90S 换")
    say()
    say("★ 顺便说明：不管底部那颗好坏，现在的接线和固件默认值不匹配：")
    say("     实际接线：底部→GPIO13，顶部→GPIO14")
    say("     固件默认：PAN→GPIO14，TILT→GPIO15")
    say()
    say("  方案 A（推荐）：改回标准接线")
    say("     底部（pan）  → GPIO14")
    say("     顶部（tilt） → GPIO15")
    say("     这样和固件默认一致，不用改代码")
    say()
    say("  方案 B：运行时改引脚（不重烧，重启失效）")
    say("     G 13     # PAN 切到 GPIO13")
    say("     T 14     # TILT 切到 GPIO14")
    say("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
