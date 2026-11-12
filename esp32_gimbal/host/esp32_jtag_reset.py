#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
esp32_jtag_reset.py — 通过 USB-JTAG 接口复位 ESP32 进入下载模式

ESP32-S3 的原生 USB 有两个功能：
    ① CDC-ACM 串口（固件输出日志用）
    ② USB-JTAG 调试接口（烧录用）

烧录标准流程需要：拉 DTR/RTS 让芯片进 bootloader。
但 CDC-ACM 的 SET_CONTROL_LINE_STATE 是否真的接到 EN/IO0 上，
取决于硬件设计——DevKitC-1 上是接了的（通过两个三极管）。

这个脚本尝试：
    ① 用 CDC 的 SET_CONTROL_LINE_STATE 拉 DTR/RTS
    ② 看芯片是否进入下载模式（USB 重新枚举）
"""
import struct
import sys
import time

import usb.core
import usb.util

VID = 0x303A
PID = 0x1001
CDC_SET_CONTROL_LINE_STATE = 0x22
CDC_SET_LINE_CODING = 0x20


def find():
    return usb.core.find(idVendor=VID, idProduct=PID)


def get_ctrl_if(dev):
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == 0x02:
                return intf
    return None


def main():
    sys.stdout.reconfigure(line_buffering=True)
    print("=" * 66)
    print("尝试通过 USB-JTAG 复位 ESP32")
    print("=" * 66)
    print()

    dev = find()
    if dev is None:
        print("❌ 找不到 ESP32 (303a:1001)")
        return 1

    print("[1] 当前设备")
    print("    VID:PID = %04x:%04x" % (dev.idVendor, dev.idProduct))
    try:
        print("    产品 = %s" % dev.product)
        print("    序列号 = %s" % dev.serial_number)
    except Exception:
        pass
    print()

    ctrl = get_ctrl_if(dev)
    if ctrl is None:
        print("❌ 没找到 CDC 控制接口")
        return 1
    cn = ctrl.bInterfaceNumber
    print("[2] CDC 控制接口: %d" % cn)
    print()

    # 尝试各种 DTR/RTS 组合
    # ESP32 下载模式：EN=0 时复位，IO0=0 时进 bootloader
    # CDC 的 SET_CONTROL_LINE_STATE: bit0=DTR, bit1=RTS
    combos = [
        ("全低 (DTR=0 RTS=0)", 0x00),
        ("DTR=1 RTS=0", 0x01),
        ("DTR=0 RTS=1", 0x02),
        ("全高 (DTR=1 RTS=1)", 0x03),
    ]

    print("[3] 尝试各种 DTR/RTS 组合（每种持续 1 秒）")
    print("    如果芯片复位，USB 会重新枚举（我们会短暂失去连接）")
    print()

    for name, val in combos:
        try:
            dev.ctrl_transfer(0x21, CDC_SET_CONTROL_LINE_STATE, val, cn, None)
            print("    %-22s → 已设置 (0x%02x)" % (name, val))
        except Exception as e:
            print("    %-22s → 失败: %s" % (name, e))
        time.sleep(1.0)

        # 检查设备是否还在
        d2 = find()
        if d2 is None:
            print("        ⚠️  设备消失（可能在复位）")
            time.sleep(1.5)
            d2 = find()
            if d2 is None:
                print("        → 设备没回来")
            else:
                print("        → 设备重新出现，序列号 %s" % getattr(d2, "serial_number", "?"))
            dev = d2
            if dev is None:
                break
            ctrl = get_ctrl_if(dev)
            cn = ctrl.bInterfaceNumber if ctrl else cn

    print()
    print("[4] 最终状态")
    d = find()
    if d is None:
        print("    ❌ 设备不在了")
    else:
        print("    ✅ 设备在: %04x:%04x" % (d.idVendor, d.idProduct))
        try:
            print("       序列号 = %s" % d.serial_number)
        except Exception:
            pass

    print()
    print("=" * 66)
    print("注意：")
    print("  ESP32-S3-DevKitC-1 的 DTR/RTS 经三极管控制 EN/IO0，")
    print("  正常组合是：DTR=0,RTS=1 → EN低(复位)；DTR=1,RTS=0 → IO0低(下载)")
    print("  如果上面的组合都没让设备重新枚举，说明 CDC 的线控没接到复位电路，")
    print("  这种情况只能按板子上的 BOOT + RST 按钮手动进下载模式。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
