#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_hw.py — 找出「舵机信号线到底落在哪个针」，并做通电响动测试

已知（2026-11-05 实测）：
    · 固件是 v2（I -> GPIN 14 15）
    · GPIO14 pad 上 PWM 完全正常：pw_avg=1499us per_avg=20000us，P 45 0 时变 1987us
    → 固件无罪、PWM 无罪，问题 100% 在硬件

    · 但 V 14 和 V 15 都报 FLOATING（上拉读到1、下拉读到0 = 那个针上什么都没接）
      SG90 的信号脚内部通常带下拉（10k 量级），真接上应该报 CONNECTED。
      所以强烈怀疑：橙线没插在 GPIO14 上，或插了但没接触。

本脚本做三件事：
    [1] 扫全部可用 GPIO，看哪个针报 CONNECTED → 信号线的真实落点
    [2] N 14 裸 GPIO 翻转（1.67Hz 方波）→ 舵机若是活的会疯狂抖/嗡嗡响
    [3] D 1 演示摆动（1000~2000us/50Hz/每边4秒）→ 正规 PWM 摆动

跑法（板子上，先停桥）：
    bash /root/esp32_gimbal/stop_bridge.sh
    /root/rknn_env/bin/python /root/esp32_gimbal/diag_hw.py
"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial

# 和固件 is_user_gpio() 白名单一致的候选脚（已排除 strapping/USB/UART/Flash）
SCAN_PINS = [1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,
             14, 15, 16, 17, 18, 21, 38, 39, 40, 41, 42, 47]


def collect(ser, dur):
    out = []
    t0 = time.time()
    while time.time() - t0 < dur:
        line = ser.readline()
        if not line:
            continue
        v = line.decode("utf-8", "replace").strip()
        if v:
            out.append(v)
    return out


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
    return "(超时)"


def main():
    print("=" * 72)
    print("舵机硬件定位 —— 信号线到底在哪个针")
    print("=" * 72)

    try:
        ser = UsbCdcSerial(timeout=0.4)
    except Exception as e:
        print("\n打不开 ESP32: %s" % e)
        print("先停桥： bash /root/esp32_gimbal/stop_bridge.sh")
        return 1

    print("已连接 %s" % ser.port)
    ser.reset_input_buffer()

    # ---------------- 1. 全引脚探测 ----------------
    print()
    print("[1] 扫描全部可用引脚，找 CONNECTED（=那个针上真的接了东西）")
    print("    " + "-" * 68)
    print("    CONNECTED  = 有外部负载/下拉 → 信号线很可能在这")
    print("    FLOATING   = 悬空，那个针上什么都没接")
    print("    HIGH_FIXED = 被拉到电源（危险，可能接错到 5V/3V3）")
    print()

    found = []
    for pin in SCAN_PINS:
        r = one(ser, "V %d" % pin, 0.7)
        # 只打印非 FLOATING 的，避免刷屏；同时打印 14/15 作为基准
        if "FLOATING" not in r or pin in (14, 15):
            print("    V %-3d -> %s" % (pin, r))
        if "CONNECTED" in r and pin not in (14, 15):
            found.append(pin)
        if "HIGH_FIXED" in r:
            print("       ⚠️ 这个针被拉到电源了，检查是不是插错到 5V/3V3")

    print()
    if found:
        print("    ★ 除 14/15 之外，这些针报 CONNECTED：%s" % found)
        print("      → 如果舵机橙线在这上面，改引脚即可：")
        print("        G %d        # 把 PAN 通道改到这个脚" % found[0])
    else:
        print("    → 除了基准脚，没有任何针报 CONNECTED。")
        print("      两种可能：")
        print("        a) 舵机信号脚是高阻输入，内部没有下拉 → FLOATING 属正常，")
        print("           那信号线是否插好只能靠肉眼/换线确认")
        print("        b) 橙线真的完全没接触到任何针")

    # ---------------- 2. 裸 GPIO 翻转：最粗暴的响动测试 ----------------
    print()
    print("[2] N 14 —— 裸 GPIO 每 300ms 翻转（1.67Hz 方波）跑 6 秒")
    print("    " + "-" * 68)
    print("    舵机若是活的、通电的，会疯狂来回抖 / 嗡嗡响（这不是正常 PWM，")
    print("    只是拿最粗暴的信号把「这舵机到底有没有电流经过」问出来）")
    print()
    print("    >>> 请现在盯着舵机、把耳朵凑近 <<<")
    print()
    time.sleep(1.0)
    r = one(ser, "N 14", 0.8)
    print("    N 14 -> %s" % r)
    collect(ser, 6.0)
    one(ser, "A", 0.6)
    print("    A    -> 停止翻转，引脚还给 LEDC")

    # ---------------- 3. 正规 PWM 摆动 ----------------
    print()
    print("[3] D 1 —— 正规 PWM 演示摆动（1000~2000us / 50Hz / 每边 4 秒）")
    print("    " + "-" * 68)
    print("    >>> 再盯 9 秒，正常舵机会缓慢左右摆 <<<")
    print()
    r = one(ser, "D 1", 0.8)
    print("    D 1 -> %s" % r)
    collect(ser, 9.0)
    one(ser, "D 0", 0.6)
    one(ser, "H", 0.8)
    print("    已停止，回中")

    # ---------------- 4. 检查 USB 还活着吗 ----------------
    print()
    print("[4] 检查通信是否还正常（如果刚才供电被拉垮，ESP32 会复位/掉线）")
    print("    " + "-" * 68)
    ser.reset_input_buffer()
    r = one(ser, "I", 1.0)
    print("    I -> %s" % r)
    if r.startswith("GPIN"):
        print("    ✅ ESP32 全程没掉线 → 5V 供电没有被舵机拉垮（至少没到复位程度）")
    else:
        print("    ❌ ESP32 可能复位过 / USB 掉了 → 强烈怀疑供电不足")

    ser.close()

    print()
    print("=" * 72)
    print("请你回答三个问题（我需要这个才能定位）")
    print("=" * 72)
    print("  ① [2] 那 6 秒里，舵机动了吗？")
    print("       完全没反应     → 没电 or 信号没到 or 舵机坏了")
    print("       抖了一下/嗡嗡  → 有电有信号，只是频率不对，硬件是通的！")
    print()
    print("  ② [3] 那 9 秒里，舵机有没有缓慢左右摆？")
    print("       有             → 一切正常，之前是别的问题")
    print("       没有           → 继续查 ③")
    print()
    print("  ③ 用手拨一下舵机摇臂（断电也可以）")
    print("       很轻松能转     → 没通电/没自锁，查 5V 和 GND")
    print("       有点阻力       → 通电了，是信号问题")
    print()
    print("  另外请肉眼确认一件事：")
    print("     橙线（信号）真的插在丝印 GPIO14 那个针上吗？")
    print("     红线 真的插在 DC 模块的 5V OUT+ 上吗？")
    print("     棕/黑线 和 ESP32 的 GND 是不是**同一根线/同一个接点**？")
    print("     ★ 最后这条最重要：ESP32 用 USB 供电、舵机用 DC 模块供电时，")
    print("       两边的 GND 必须物理连在一起，否则 PWM 信号没有共同参考点，")
    print("       舵机收到的是乱码 —— 表现正好就是「完全不锁、一点声音没有」。")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
