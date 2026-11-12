#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""servo_live.py — 让舵机持续摆动，人随时看都行（不用对倒计时）

为什么用这个而不是 servo_bringup.py：
    带倒计时的脚本需要人「在正确的那几秒盯着舵机」，
    但我们的对话是有延迟的 —— 我说「开始看」的时候测试可能已经跑完了。
    所以这里改成：发一条 D 1 让舵机**无限连续摆动**，然后立刻退出。
    ESP32 的主循环会一直 update_demo() 直到收到 D 0，
    人什么时候去看都行，看够了再说停。

用法：
    /root/rknn_env/bin/python servo_live.py          # 开始连续摆动
    /root/rknn_env/bin/python servo_live.py stop     # 停下回中

接线（本次要验证的目标状态）：
    舵机 3 孔壳整排套到左侧排针：  GPIO14 / 5Vin / GND
        橙线（信号） -> GPIO14
        红线（电源） -> 5Vin      ← 从 DC 模块改回来
        棕线（地）   -> GND       ← 和 ESP32 同一块板，天然共地
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


def one(ser, cmd, wait=1.0):
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


def line(txt=""):
    print(txt)


def main():
    stop_mode = len(sys.argv) > 1 and sys.argv[1].lower() == "stop"

    print("=" * 70)
    if stop_mode:
        print("停止摆动并回中")
    else:
        print("让舵机持续摆动（之后任何时候去看都行）")
    print("=" * 70)

    try:
        ser = UsbCdcSerial(timeout=0.4)
    except Exception as e:
        print("\n打不开 ESP32: %s" % e)
        print("先停桥： bash /root/esp32_gimbal/stop_bridge.sh")
        return 1

    print("已连接 %s" % ser.port)
    ser.reset_input_buffer()

    if stop_mode:
        one(ser, "D 0", 0.8)
        one(ser, "H", 1.0)
        print("  已停止，已回中")
        r = one(ser, "I", 1.2)
        print("  I -> %s" % r)
        ser.close()
        return 0

    # ---------- 1. 固件自检 ----------
    print()
    print("[1] 固件自检")
    r = one(ser, "I", 1.2)
    print("    I -> %s" % r)
    if not r.startswith("GPIN"):
        print("    ❌ 通信异常，先解决这个")
        ser.close()
        return 1

    # ---------- 2. 引脚探测：看接线有没有变化 ----------
    print()
    print("[2] 引脚探测（对比上次：上次 14 和 15 都是 FLOATING）")
    for pin in [14, 15, 13, 12, 16]:
        r = one(ser, "V %d" % pin, 1.0)
        mark = ""
        if "CONNECTED" in r:
            mark = "  ★ 有负载！"
        elif "FLOATING" in r:
            mark = "  (悬空)"
        elif "HIGH_FIXED" in r:
            mark = "  ⚠️ 被拉到电源"
        print("    V %-3d -> %s%s" % (pin, r, mark))

    # ---------- 3. 确认 PWM 还在 ----------
    print()
    print("[3] 确认 PWM 仍然正常")
    one(ser, "P 0 0", 0.8)
    ser.reset_input_buffer()
    ser.write(b"R 14\n")
    ser.flush()
    time.sleep(0.4)
    lines = collect(ser, 2.5)
    lb = [x for x in lines if "pw_avg" in x]
    if lb:
        print("    %s" % lb[-1])
        print("    ✅ PWM 正常")
    else:
        print("    ⚠️ 没收到 LB：%r" % lines[-3:])
    ser.write(b"R -1\n")
    ser.flush()
    time.sleep(0.3)
    ser.reset_input_buffer()

    # ---------- 4. 启动连续摆动，然后退出 ----------
    print()
    print("[4] 启动 D 1 —— 舵机开始无限连续摆动（每边 4 秒）")
    r = one(ser, "D 1", 1.0)
    print("    D 1 -> %s" % r)

    # 确认它真的在跑（看 LB 之外再看一次状态）
    time.sleep(0.5)
    ser.close()

    print()
    print("=" * 70)
    print("  ★★★ 现在去看舵机 ★★★")
    print("=" * 70)
    print("  它应该正在缓慢左右摆（1000us→2000us→1000us，每边 4 秒）。")
    print("  这个摆动会一直持续，不用赶时间。")
    print()
    print("  三种可能的观察结果：")
    print("    ① 在正常左右摆         → 通了！之前就是 DC 模块没共地")
    print("    ② 完全不动、也没声音   → 见下方排查")
    print("    ③ 抖了一下 / 嗡嗡响    → 有电，但可能供电不足在打嗝")
    print()
    print("  停下来： /root/rknn_env/bin/python servo_live.py stop")
    print()
    print("  如果 ② 完全不动，接下来查这两个（各试一次）：")
    print("    a) 舵机壳整体偏移一格 → 用 W 命令三针同时驱动，一次覆盖：")
    print("         W 14 13 12      （把信号同时给 14/13/12）")
    print("       恢复： W 0")
    print("    b) 舵机坏了 → 换一个舵机，或者插回昨天能用的位置")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
