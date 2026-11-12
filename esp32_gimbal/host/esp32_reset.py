#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 USB CDC 触发 ESP32 复位（烧录后必须复位才能跑新固件）"""
import struct
import sys
import time

import usb.core
import usb.util

VID, PID = 0x303A, 0x1001
CDC_SET_CONTROL_LINE_STATE = 0x22


def main():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print("找不到 ESP32")
        return 1

    ctrl = None
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == 0x02:
                ctrl = intf
    if ctrl is None:
        print("找不到 CDC 控制接口")
        return 1

    cn = ctrl.bInterfaceNumber
    print("CDC 控制接口: %d" % cn)
    print()

    # DevKitC-1 的复位电路：DTR/RTS 经三极管控制 EN 和 IO0
    # 标准复位序列（esptool 用的）：
    #   RTS=1 (EN低,复位) → RTS=0 (释放)
    # 注意 CDC 的 bit0=DTR, bit1=RTS
    sequences = [
        ("RTS 拉低 (EN复位)", 0x02, 0.2),
        ("全部释放", 0x00, 0.3),
        ("DTR=1 RTS=1", 0x03, 0.2),
        ("全部释放", 0x00, 0.5),
    ]

    for name, val, wait in sequences:
        try:
            dev.ctrl_transfer(0x21, CDC_SET_CONTROL_LINE_STATE, val, cn, None)
            print("  %-20s → 0x%02x" % (name, val))
        except Exception as e:
            print("  %-20s → 失败: %s" % (name, e))
        time.sleep(wait)

    print()
    print("复位序列已发送。等待 2 秒后重新连接...")
    time.sleep(2.0)

    # 重连并读启动日志
    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    time.sleep(0.5)

    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print("设备不在了")
        return 1

    data_if = ctrl_if = None
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == 0x0A:
                data_if = intf
            elif intf.bInterfaceClass == 0x02:
                ctrl_if = intf

    ep_in = None
    for ep in data_if:
        if (ep.bmAttributes & 3) == 2 and (ep.bEndpointAddress & 0x80):
            ep_in = ep

    try:
        dev.set_configuration()
    except Exception:
        pass

    lc = struct.pack("<IBBB", 115200, 0, 0, 8)
    try:
        dev.ctrl_transfer(0x21, 0x20, 0, ctrl_if.bInterfaceNumber, lc)
        dev.ctrl_transfer(0x21, CDC_SET_CONTROL_LINE_STATE, 0x03,
                          ctrl_if.bInterfaceNumber, None)
    except Exception:
        pass

    print()
    print("=== 读启动日志（3 秒）===")
    t0 = time.time()
    total = 0
    while time.time() - t0 < 3.0:
        try:
            d = ep_in.read(512, timeout=200)
            if len(d):
                total += len(d)
                txt = bytes(d).decode("utf-8", "replace").rstrip()
                print("  %s" % txt[:200])
        except usb.core.USBError as e:
            if getattr(e, "errno", None) != 110:
                pass

    print()
    print("共收到 %d 字节" % total)
    if total > 0:
        print("✅ 有启动日志 —— 复位成功")
    else:
        print("⚠️  没有日志。如果按 RST 也没用，说明线控没接到复位电路")
        print("   → 请手动按 ESP32 板上的 RST 键")

    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
