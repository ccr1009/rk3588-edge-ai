#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
catch_boot_full.py — 持续读 USB CDC 直到没有新数据，抓完整启动日志

之前抓不全的原因：
    USB CDC 的数据不是"推"过来的，得一直读。
    而且复位瞬间设备会重新枚举，句柄失效 —— 必须能重连。
    这里做：一直读 + 断开就重连 + 读完静默 2 秒才停。
"""
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
    if data_if is None:
        return None
    ep_in = None
    for ep in data_if:
        if (ep.bmAttributes & 3) == 2 and (ep.bEndpointAddress & 0x80):
            ep_in = ep
    if ep_in is None:
        return None
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
    try:
        dev.ctrl_transfer(0x21, 0x20, 0, cn,
                          struct.pack("<IBBB", 115200, 0, 0, 8))
        dev.ctrl_transfer(0x21, 0x22, 0x03, cn, None)
    except Exception:
        pass
    return dev, ep_in, ep_out_of(data_if)


def ep_out_of(data_if):
    for ep in data_if:
        if (ep.bmAttributes & 3) == 2 and not (ep.bEndpointAddress & 0x80):
            return ep
    return None


def main():
    print("=" * 66)
    print("抓完整启动日志 —— 请按 ESP32 的 RST")
    print("=" * 66)
    print()

    t_start = time.time()
    buf = b""
    last_data_t = 0.0
    handle = None
    total = 0

    while time.time() - t_start < 25.0:
        if handle is None:
            h = open_dev()
            if h is None:
                time.sleep(0.15)
                continue
            handle = h

        dev, ep_in, ep_out = handle
        got_any = False
        try:
            for _ in range(20):
                d = ep_in.read(512, timeout=60)
                if len(d):
                    got_any = True
                    total += len(d)
                    buf += bytes(d)
        except usb.core.USBError as e:
            if getattr(e, "errno", None) != 110:
                # 设备掉了，重连
                try:
                    usb.util.dispose_resources(dev)
                except Exception:
                    pass
                handle = None
                time.sleep(0.1)
                continue

        if got_any:
            last_data_t = time.time()

        # 如果已经收到数据、且静默 2 秒，认为启动完成
        if total > 0 and time.time() - last_data_t > 2.0:
            break

        time.sleep(0.02)

    if handle is not None:
        try:
            usb.util.dispose_resources(handle[0])
        except Exception:
            pass

    print("=== 收到的原始内容 ===")
    print(buf.decode("utf-8", "replace"))
    print()
    print("=" * 66)
    print("共 %d 字节" % total)
    print()
    text = buf.decode("utf-8", "replace")
    checks = {
        "BOOT gimbal v2": "BOOT gimbal v2" in text,
        "BOOT gimbal (任意)": "BOOT gimbal" in text,
        "READY pan=": "READY pan=" in text,
        "app_init": "app_init" in text,
    }
    for k, v in checks.items():
        print("  %-22s %s" % (k, "✅" if v else "✗"))
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
