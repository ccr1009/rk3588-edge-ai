#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""servo_bringup.py — 带大号倒计时的舵机通电测试（要盯着舵机看）

为什么要有这个脚本：
    前面 diag_usb.py / diag_hw.py 已经证明【软件侧全部正常】：
        · 固件 v2        I -> GPIN 14 15
        · GPIO14 上有真波形  pw_avg=1499us  per_avg=20000us
        · 改角度脉宽会变   P 45 0 -> pw_avg=1987us
    所以现在唯一的问题是硬件。但硬件测试必须「人盯着舵机」才有意义，
    之前脚本跑完才打印提示，人容易错过那几秒关键动作。

    这份脚本改成：先倒数 5 秒准备，再分段做两种完全不同性质的刺激，
    每段前后都有明显的文字标记，确保人能对上号。

    [相位1] 静默 3 秒      —— 基线，正常情况下舵机应该有自锁力、微微嗡
    [相位2] N 14 粗暴方波  —— 1.67Hz 上下翻转。舵机活着会疯狂抖/咔咔响
    [相位3] 静默 2 秒      —— 区分相位2的余振
    [相位4] D 1 正规摆动   —— 1000~2000us / 50Hz / 每边4秒，缓慢左右摆
    [相位5] 停 + 回中

跑法（板子上，先停桥）：
    bash /root/esp32_gimbal/stop_bridge.sh
    /root/rknn_env/bin/python /root/esp32_gimbal/servo_bringup.py

改完接线就重跑一次，对比结果。
"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial


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


def banner(txt):
    print()
    print("=" * 72)
    print("  " + txt)
    print("=" * 72)


def countdown(n, note=""):
    """大号倒数，确保人能看到并准备好"""
    for i in range(n, 0, -1):
        sys.stdout.write("\r    >>> %d ...  %s          " % (i, note))
        sys.stdout.flush()
        time.sleep(1.0)
    sys.stdout.write("\r    >>> 开始！                        \n")
    sys.stdout.flush()


def main():
    banner("舵机通电测试（请务必盯着舵机看 / 把耳朵凑近）")

    try:
        ser = UsbCdcSerial(timeout=0.4)
    except Exception as e:
        print("\n打不开 ESP32: %s" % e)
        print("先停桥： bash /root/esp32_gimbal/stop_bridge.sh")
        return 1

    print("已连接 %s" % ser.port)
    ser.reset_input_buffer()

    # ---------- 前置自检：软件侧正常吗 ----------
    print()
    print("[前置自检] 确认 PWM 仍在输出（如果这里就不对，别测硬件了）")
    r = one(ser, "I", 1.0)
    print("    I -> %s" % r)
    if not r.startswith("GPIN"):
        print("    ❌ 固件通信异常，先解决通信")
        return 1

    one(ser, "P 0 0", 0.8)
    ser.reset_input_buffer()
    ser.write(b"R 14\n")
    ser.flush()
    time.sleep(0.4)
    lines = collect(ser, 2.5)
    lb = [x for x in lines if "pw_avg" in x]
    if lb:
        print("    %s" % lb[-1])
        print("    ✅ PWM 正常（pw_avg 应 ≈1499us）")
    else:
        print("    ⚠️ 没收到 LB 行：%r" % lines[-3:])
    ser.write(b"R -1\n")
    ser.flush()
    time.sleep(0.3)
    ser.reset_input_buffer()

    # ---------- 相位 1：静默基线 ----------
    banner("相位 1 / 5   静默 3 秒 —— 基线观察")
    print("  正常通电的舵机：有自锁力（手指推不动），贴近听有极轻微嗡嗡声")
    print("  完全没通电的舵机：松垮、无声")
    one(ser, "H", 0.5)
    countdown(3, "现在贴近听舵机有没有嗡嗡声")
    print("    基线结束")
    print()

    # ---------- 相位 2：粗暴方波 ----------
    banner("相位 2 / 5   N 14  粗暴方波 6 秒 —— 最粗暴的『有没有电』测试")
    print("  这是 1.67Hz 的 GPIO 翻转，不是合法舵机信号。")
    print("  · 舵机通电 → 会疯狂来回抖、发出咔咔/嗡嗡声（幅度可能很大）")
    print("  · 完全不响 → 舵机没电，或信号线根本没接到 GPIO14")
    countdown(5, "手离开舵机，准备看它抖")
    one(ser, "N 14", 0.8)
    collect(ser, 6.0)
    one(ser, "A", 0.6)
    print("    方波结束")

    # ---------- 相位 3：静默 ----------
    banner("相位 3 / 5   静默 2 秒 —— 让抖动停下，区分是否只是余振")
    time.sleep(2.0)
    print("    静默结束")

    # ---------- 相位 4：正规 PWM 摆动 ----------
    banner("相位 4 / 5   D 1  正规 PWM 摆动 9 秒 —— 正常舵机会缓慢左右摆")
    print("  1000~2000us / 50Hz / 每边 4 秒，和市面上 Arduino 例程同参数。")
    print("  · 正常 → 会看到摇臂缓慢扫过约 180°，回摆，再扫")
    print("  · 只抖不摆 / 完全不动 → 信号质量或供电问题")
    countdown(4, "盯住摇臂")
    one(ser, "D 1", 0.8)
    collect(ser, 9.0)
    one(ser, "D 0", 0.6)

    # ---------- 相位 5：收尾 ----------
    banner("相位 5 / 5   停止并回中")
    one(ser, "H", 1.0)
    print("    已回中")

    # ---------- 通信存活检查 ----------
    print()
    print("[存活检查] 如果供电被舵机拉垮，ESP32 会复位/掉线")
    ser.reset_input_buffer()
    r = one(ser, "I", 1.5)
    print("    I -> %s" % r)
    if r.startswith("GPIN"):
        print("    ✅ ESP32 全程没掉线 → 供电至少没到让 ESP32 复位的程度")
    else:
        print("    ❌ ESP32 掉线/复位了 → 供电严重不足，或共地有问题")

    ser.close()

    # ---------- 结论表 ----------
    banner("请对照回答（这是定位的最后一块拼图）")
    print("  相位 1（静默 3s）")
    print("     有自锁力 / 有嗡嗡声  → 舵机通电了 ★★★")
    print("     松垮无声             → 没通电 ★★★")
    print()
    print("  相位 2（粗暴方波 6s）")
    print("     疯狂抖 / 咔咔响      → 有电、信号也到了，硬件是通的")
    print("     完全不动             → 没电，或橙线没接到 GPIO14")
    print()
    print("  相位 4（正规摆动 9s）")
    print("     缓慢左右摆           → 一切正常，已经可以用了")
    print("     不动 / 只抖          → 继续查供电与共地")
    print()
    print("=" * 72)
    print("  接线检查清单（按重要性排序）")
    print("=" * 72)
    print("  ① 共地 —— 最常见的坑")
    print("     ESP32 是 USB 供电，舵机是 DC 模块供电。")
    print("     这两个电源的 GND 必须用一根线连在一起！")
    print("     DC 模块 OUT- ──── 一根线 ──── ESP32 GND 针")
    print("     没有这根线，PWM 信号没有共同参考电平，")
    print("     舵机收到的是乱码 —— 症状就是「完全没锁、一点声音都没有」。")
    print()
    print("  ② 5V 到底有没有到舵机")
    print("     红线接 DC 模块 OUT+，棕线接 OUT-。")
    print("     如果 DC 模块标称 5V 但其实没输出（或输出低于 4.5V），")
    print("     舵机内部电路不工作 → 完全无声。")
    print("     最快的验证：把红线改插 ESP32 的 5Vin、棕线插 ESP32 GND，")
    print("     让 ESP32 一块板子供它。能动了就说明是 DC 模块的问题。")
    print()
    print("  ③ 橙线（信号）位置")
    print("     必须在丝印 GPIO14（紧挨着 5Vin 和 GND 的那三个针里）。")
    print("     3 孔塑料壳如果整体偏移一格，就会插到隔壁针上。")
    print("     固件有 W 命令可以三针同时驱动，一次性覆盖：")
    print("        W 14 13 12")
    print("     如果舵机在隔壁针上，这样就通了。恢复：W 0")
    print()
    print("  ④ 舵机本身")
    print("     换个舵机试。或者把这个舵机插回昨天能用的那个接线位置。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
