#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓完整启动日志，看固件版本和分区信息"""
import struct
import sys
import time

import usb.core
import usb.util

VID, PID = 0x303A, 0x1001


def open_dev():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        return None
    data_if = ctrl_if = None
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == 0x0A:
                data_if = intf
            elif intf.bInterfaceClass == 0x02:
                ctrl_if = intf
    ep_in = ep_out = None
    if data_if:
        for ep in data_if:
            if (ep.bmAttributes & 3) == 2:
                if ep.bEndpointAddress & 0x80:
                    ep_in = ep
                else:
                    ep_out = ep
    for intf in (data_if, ctrl_if):
        if intf is None:
            continue
        try:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)
        except Exception:
            pass
    try:
        dev.set_configuration()
    except Exception:
        pass
    cn = ctrl_if.bInterfaceNumber if ctrl_if else 0
    lc = struct.pack("<IBBB", 115200, 0, 0, 8)
    try:
        dev.ctrl_transfer(0x21, 0x20, 0, cn, lc)
        dev.ctrl_transfer(0x21, 0x22, 0x03, cn, None)
    except Exception:
        pass
    return dev, ep_in, ep_out, cn


def main():
    h = open_dev()
    if h is None:
        print("找不到 ESP32")
        return 1
    dev, ep_in, ep_out, cn = h

    print("=== 触发复位 ===")
    try:
        dev.ctrl_transfer(0x21, 0x22, 0x02, cn, None)
        time.sleep(0.2)
        dev.ctrl_transfer(0x21, 0x22, 0x00, cn, None)
        time.sleep(0.3)
        print("    复位信号已发")
    except Exception as e:
        print("    复位失败: %s" % e)

    # 等待重新枚举
    time.sleep(1.5)
    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass

    print()
    print("=== 抓启动日志（8 秒）===")
    t0 = time.time()
    total = 0
    buf = b""
    while time.time() - t0 < 8.0:
        h = open_dev()
        if h is None:
            time.sleep(0.2)
            continue
        dev, ep_in, ep_out, cn = h
        try:
            while True:
                d = ep_in.read(512, timeout=150)
                if len(d):
                    total += len(d)
                    buf += bytes(d)
        except usb.core.USBError as e:
            if getattr(e, "errno", None) != 110:
                pass
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass

    text = buf.decode("utf-8", "replace")
    print(text)
    print()
    print("=" * 60)
    print("共 %d 字节" % total)
    print()
    # 关键信息提取
    for key in ["BOOT gimbal", "READY pan=", "partition", "app_init", "Project name"]:
        for line in text.splitlines():
            if key in line:
                print("  [%s] %s" % (key, line.strip()))
                break
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
