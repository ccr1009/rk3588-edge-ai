#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_usb.py — 绕过桥接，直连 ESP32 USB CDC，把「波形」这件事一次问清

为什么必须绕开桥（2026-11-05）：
    gimbal_bridge.py 是「发一行 → 读一行」的一问一答结构。
    但固件的 R（回环自测）是**异步推送**：回了 "LB START 14" 之后，
    每秒往串口推一行 "LB pin=14 edges=... pw_avg=...us"，
    这些行没有任何请求对应，桥根本不会去读，所以永远看不到。

    同理 V（引脚探测）、X（扫描）也有异步输出。

    这份脚本直接用 pyusb 操作 CDC 端点，能收到所有异步行。

跑法（板子上，先把桥停掉，USB 设备独占）：
    bash /root/esp32_gimbal/stop_bridge.sh
    /root/rknn_env/bin/python /root/esp32_gimbal/diag_usb.py
"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial


def collect(ser, dur):
    """收满 dur 秒的所有行"""
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


def ask(ser, cmd, wait=0.6, dur=0.0):
    ser.write((cmd + "\n").encode())
    ser.flush()
    time.sleep(wait)
    if dur > 0:
        return collect(ser, dur)
    return collect(ser, 0.0) or []


def one(ser, cmd, wait=0.6):
    """发一条，返回第一行"""
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
    print("=" * 70)
    print("ESP32 云台 —— 直连 USB 诊断（绕开桥接）")
    print("=" * 70)

    try:
        ser = UsbCdcSerial(timeout=0.4)
    except Exception as e:
        print("\n打不开 ESP32 USB: %s" % e)
        print("先停桥： bash /root/esp32_gimbal/stop_bridge.sh")
        return 1

    print("已连接 %s" % ser.port)

    # 清掉上电残留
    ser.reset_input_buffer()

    # ---------- 1. 固件身份 ----------
    print()
    print("[1] 固件身份")
    print("    " + "-" * 62)
    for cmd in ["I", "Q"]:
        print("    %-6s -> %r" % (cmd, one(ser, cmd, 1.0)))

    # ---------- 2. 引脚探测：GPIO14 上到底有没有东西 ----------
    print()
    print("[2] 引脚电平探测 V —— 判断 3 孔壳有没有真的套在 GPIO14 上")
    print("    " + "-" * 62)
    print("    判读：")
    print("      CONNECTED   上拉读0/下拉读0 → 有东西接着（正常，舵机信号脚带下拉）")
    print("      FLOATING    上拉读1/下拉读0 → 那个针上什么都没接  ★重点怀疑")
    print("      HIGH_FIXED  上拉读1/下拉读1 → 被拉到 3V3/5V，接错针了")
    print()
    for pin in [14, 15]:
        r = one(ser, "V %d" % pin, 1.2)
        print("    V %-3d -> %s" % (pin, r))

    # ---------- 3. 回环自测：PWM 到底出没出到引脚 ----------
    print()
    print("[3] 回环自测 R —— 决定性一步")
    print("    " + "-" * 62)
    print("    原理：引脚同时开输出(供 LEDC 驱动)和输入(读回 pad 真实电平)，")
    print("          用边沿中断 + 微秒时间戳算脉宽。量的是 pad 上的真电平。")
    print()

    one(ser, "P 0 0", 0.8)
    ser.reset_input_buffer()

    ser.write(b"R 14\n")
    ser.flush()
    time.sleep(0.4)
    print("    收集 5 秒统计（固件每秒推一次）...")
    lines = collect(ser, 5.0)
    lb = [x for x in lines if "LB" in x or "edges" in x]
    if lb:
        for x in lb[-6:]:
            print("      %s" % x)
    else:
        print("      ❌ 一行 LB 都没收到！原始输出：%r" % lines[-6:])

    verdict_ok = False
    joined = " ".join(lb)
    if "edges=0" in joined or "NO_SIGNAL" in joined:
        print()
        print("    ❌ GPIO14 上 0 个边沿 = 引脚根本没有波形")
        print("       → 固件/引脚配置问题，跟舵机、供电、杜邦线全都没关系")
        print("       → 下一步：发 N 14 进入裸 GPIO 翻转（300ms 方波），A 停止")
    elif "pw_avg" in joined:
        verdict_ok = True
        print()
        print("    ✅ 有波形！对照下面的期望值：")
        print("       P 0 0    → pw_avg 应该 ≈1500us，per_avg ≈20000us")
        print("       P 45 0   → pw_avg 应该 ≈2000us（或 ≈1000us，看方向）")
        print("       若对得上 → 信号 100% 出到 pad 了，问题只在硬件侧")

    ser.write(b"R -1\n")
    ser.flush()
    time.sleep(0.3)
    ser.reset_input_buffer()

    # ---------- 4. 满量程验证 ----------
    if verdict_ok:
        print()
        print("[4] 满量程验证 P 45 0 —— 脉宽应该从 1500us 明显变化")
        print("    " + "-" * 62)
        one(ser, "P 45 0", 0.8)
        ser.write(b"R 14\n")
        ser.flush()
        time.sleep(0.4)
        lines2 = collect(ser, 3.5)
        lb2 = [x for x in lines2 if "LB" in x or "edges" in x]
        for x in lb2[-4:]:
            print("      %s" % x)
        if not lb2:
            print("      (没收到 LB 行)")
        ser.write(b"R -1\n")
        ser.flush()
        time.sleep(0.3)
        ser.reset_input_buffer()

    # ---------- 5. 归位 ----------
    one(ser, "H", 1.0)
    one(ser, "S 0", 0.4)
    ser.close()

    # ---------- 结论 ----------
    print()
    print("=" * 70)
    print("结论")
    print("=" * 70)
    if verdict_ok:
        print("  固件没问题、PWM 确实出到 GPIO14 了。")
        print("  舵机不动只能从这三处查（按怀疑度排序）：")
        print()
        print("  ① 共地（最常见，占一半）")
        print("     舵机 GND 和 ESP32 GND 必须是同一条地。")
        print("     你把舵机接到独立 DC 电源了 —— 那 DC 模块的 OUT- ")
        print("     必须有一根线接到 ESP32 的 GND。没有这条线，")
        print("     信号是「悬空参考」，舵机收到的是乱码，表现就是全软+不响。")
        print()
        print("  ② 供电电流不够")
        print("     SG90 堵转 700mA 以上，启动瞬间更高。")
        print("     如果 DC 模块是小电流的（比如 5V/500mA），")
        print("     一给信号电压就塌，舵机抖一下就没劲了。")
        print("     症状：手指拨一下摇臂能拨动（没自锁力）。")
        print()
        print("  ③ 信号线断/插错针")
        print("     橙线（信号）必须在 GPIO14。")
        print("     刚才 V 14 的结果如果是 FLOATING，就是这条。")
    else:
        print("  GPIO14 没有波形 —— 这是固件/引脚问题，先别管舵机。")
        print("  下一步：N 14 裸翻转，用万用表量有没有 0↔3.3V 跳变。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
