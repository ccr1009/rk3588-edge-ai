#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_link.py — 确认 RK3588 能不能和 ESP32 通上话

只做一件事：发几条最基本的命令，看有没有正确回应。
不做任何设置、不摆舵机，纯连通性验证。

跑法：
    /root/rknn_env/bin/python /root/esp32_gimbal/check_link.py
"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")


def main():
    print("=" * 60)
    print("RK3588  <->  ESP32 连通性检查")
    print("=" * 60)

    # 1. 设备在不在
    try:
        import usb.core
    except ImportError:
        print("[1] ❌ 缺 pyusb")
        return 1

    dev = usb.core.find(idVendor=0x303A, idProduct=0x1001)
    if dev is None:
        print("[1] ❌ 没找到 ESP32 (303a:1001)")
        print("    → 检查 ESP32 的 USB 线有没有插到 RK3588 上")
        return 1
    print("[1] ✅ 找到设备  Bus %s Device %s  %04x:%04x"
          % (dev.bus, dev.address, dev.idVendor, dev.idProduct))

    # 2. 打开串口
    try:
        from usb_cdc_serial import UsbCdcSerial
        ser = UsbCdcSerial(timeout=0.5)
    except Exception as e:
        print("[2] ❌ 打不开 USB CDC: %s" % e)
        print("    → 可能桥接服务还占着设备？跑 stop_bridge.sh")
        return 1
    print("[2] ✅ USB CDC 已打开  %s" % ser.port)

    # 3. 收发测试
    print("[3] 命令往返测试")
    print("    " + "-" * 54)

    def one(cmd, wait=1.0):
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

    ser.reset_input_buffer()
    results = {}
    for cmd, expect in [("I", "GPIN"), ("Q", "S "), ("P 0 0", "OK")]:
        r = one(cmd, 1.5)
        ok = r is not None and r.startswith(expect)
        results[cmd] = (r, ok)
        print("    %-8s -> %-22r %s" % (cmd, r, "✅" if ok else "❌"))

    ser.close()

    # 4. 结论
    print()
    print("=" * 60)
    all_ok = all(v[1] for v in results.values())
    if all_ok:
        print("✅ 链路正常：RK3588 能控制 ESP32")
        print()
        print("   固件是 v2，PWM 已在此前实测正常：")
        print("   pw_avg=1499us  per_avg=20000us（50Hz 标准舵机信号）")
        print()
        print("   所以舵机不动只能是硬件侧 —— 用万用表量这 5 个点：")
        print("     ├─ 测1  舵机电源电压（黑→棕线，红→红线）     期望 4.8~5.2V")
        print("     ├─ 测2  共地通断（黑→电源OUT-，红→ESP32 GND） 期望 响 ★")
        print("     ├─ 测3  橙线通断（黑→橙线，红→GPIO14）        期望 响")
        print("     ├─ 测4  PWM 电压（黑→GND，红→橙线，P 0 0）    期望 ≈0.25V")
        print("     └─ 测5  带载最低电压（摆动时看）              期望 ≥4.5V")
        print()
        print("   详细步骤见 MEASURE_GUIDE.md")
    else:
        bad = [k for k, v in results.items() if not v[1]]
        print("❌ 有命令失败: %s" % bad)
        print()
        if results.get("I", (None,))[0] is None:
            print("   全部无回应 → ESP32 可能没在跑固件（复位循环 / 卡死）")
            print("   试试： bash /root/esp32_gimbal/restart_bridge.sh")
        else:
            print("   部分回应 → USB 通路不稳，或固件状态异常")
    print("=" * 60)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
