#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
esp32_usb_rpc.py — 通过 USB CDC 给 ESP32 发命令（pyusb 实现）

用途：在 cdc_acm 驱动缺失的情况下，用用户态 libusb 实现"串口通信"。

为什么只发 Q 测：Q 是无副作用的查询命令，最安全。
"""
import struct
import sys
import time

import usb.core
import usb.util

VID = 0x303A
PID = 0x1001
CDC_SET_LINE_CODING = 0x20
CDC_SET_CONTROL_LINE_STATE = 0x22
LINE_CODING = struct.pack("<IBBB", 115200, 0, 0, 8)


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
    ep_in = ep_out = None
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
    try:
        dev.ctrl_transfer(0x21, CDC_SET_LINE_CODING, 0, cn, LINE_CODING)
        dev.ctrl_transfer(0x21, CDC_SET_CONTROL_LINE_STATE, 0x03, cn, None)
    except Exception as e:
        print("  (line coding 设置失败: %s)" % e)
    return dev, ep_in, ep_out


def read_available(ep_in, wait=1.0):
    """读所有可用数据，直到超时"""
    buf = b""
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            d = ep_in.read(512, timeout=200)
            if len(d):
                buf += bytes(d)
        except usb.core.USBError as e:
            if getattr(e, "errno", None) == 110 or "timeout" in str(e).lower():
                continue
            raise
    return buf


def main():
    h = open_dev()
    if h is None:
        print("❌ 打不开 ESP32 USB-CDC")
        return 1
    dev, ep_in, ep_out = h
    print("✅ 已连接  bulk IN=0x%02x  OUT=0x%02x"
          % (ep_in.bEndpointAddress, ep_out.bEndpointAddress))
    print()

    # 先清空残留
    read_available(ep_in, wait=0.5)
    print("已清空缓冲")
    print()

    commands = ["Q", "I", "Q"]
    for cmd in commands:
        payload = (cmd + "\n").encode()
        print("--> %r" % cmd)
        try:
            n = ep_out.write(payload, timeout=1000)
            print("    写入 %d 字节" % n)
        except Exception as e:
            print("    ❌ 写失败: %s" % e)

        resp = read_available(ep_in, wait=1.2)
        if resp:
            p = sum(1 for c in resp if 32 <= c < 127 or c in (9, 10, 13))
            tag = "文本" if p / len(resp) > 0.5 else "二进制"
            print("    <-- %s(%d): %s"
                  % (tag, len(resp), resp.decode("utf-8", "replace").rstrip()))
        else:
            print("    <-- (无回复)")
        print()

    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
