#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
esptool_over_usb.py — 让 esptool 能通过 USB CDC 烧录 ESP32

思路：
    esptool 需要 /dev/tty* 串口，但内核没编 CONFIG_USB_ACM，建不出来。
    这里用 pty（伪终端）骗过 esptool：
      · 创建一个 /dev/pts/N，让 esptool 以为这是串口
      · 另开一个线程，把 pty 上的读写转成 USB CDC 的 bulk 端点操作

风险提示：
    esptool 烧录需要精确的时序控制（复位、bootloader 握手、CRC）。
    USB CDC 的延迟特性可能导致握手失败。
    这属于"能试但要接受失败"的方案。
"""
import os
import select
import struct
import sys
import threading
import time

import usb.core
import usb.util

VID = 0x303A
PID = 0x1001
CDC_SET_LINE_CODING = 0x20
CDC_SET_CONTROL_LINE_STATE = 0x22
LINE_CODING = struct.pack("<IBBB", 115200, 0, 0, 8)


def open_cdc(baud=115200):
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise IOError("找不到 ESP32 USB 设备")

    data_if = ctrl_if = None
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == 0x0A:
                data_if = intf
            elif intf.bInterfaceClass == 0x02:
                ctrl_if = intf
    if data_if is None:
        raise IOError("找不到 CDC Data 接口")

    ep_in = ep_out = None
    for ep in data_if:
        if (ep.bmAttributes & 3) == 2:
            if ep.bEndpointAddress & 0x80:
                ep_in = ep
            else:
                ep_out = ep
    if ep_in is None or ep_out is None:
        raise IOError("找不到 bulk 端点")

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
    lc = struct.pack("<IBBB", baud, 0, 0, 8)
    try:
        dev.ctrl_transfer(0x21, CDC_SET_LINE_CODING, 0, cn, lc)
        dev.ctrl_transfer(0x21, CDC_SET_CONTROL_LINE_STATE, 0x03, cn, None)
    except Exception as e:
        print("[warn] line coding: %s" % e)

    return dev, ep_in, ep_out


def main():
    sys.stdout.reconfigure(line_buffering=True)

    # 1. 开 USB
    print("[1] 连接 ESP32 USB CDC ...")
    dev, ep_in, ep_out = open_cdc()
    print("    已连接  IN=0x%02x OUT=0x%02x"
          % (ep_in.bEndpointAddress, ep_out.bEndpointAddress))

    # 2. 建 pty
    master_fd, slave_fd = os.openpty()
    slave_name = os.ttyname(slave_fd)
    print("[2] 创建伪终端: %s" % slave_name)

    # 把 slave 设为 raw 模式
    import tty
    tty.setraw(slave_fd)
    tty.setraw(master_fd)

    # 3. 转发线程
    stop = threading.Event()

    def pty_to_usb():
        """pty 写入（esptool 发的）→ USB bulk OUT"""
        while not stop.is_set():
            try:
                r, _, _ = select.select([master_fd], [], [], 0.1)
                if not r:
                    continue
                data = os.read(master_fd, 1024)
                if not data:
                    continue
                for i in range(0, len(data), 64):
                    ep_out.write(data[i:i + 64], timeout=500)
            except Exception as e:
                if not stop.is_set():
                    print("[pty->usb] %s" % e)

    def usb_to_pty():
        """USB bulk IN（ESP32 回的）→ pty"""
        while not stop.is_set():
            try:
                data = ep_in.read(512, timeout=100)
                if len(data):
                    os.write(master_fd, bytes(data))
            except usb.core.USBError as e:
                if getattr(e, "errno", None) != 110 and "timeout" not in str(e).lower():
                    if not stop.is_set():
                        print("[usb->pty] %s" % e)
                continue
            except Exception as e:
                if not stop.is_set():
                    print("[usb->pty] %s" % e)

    t1 = threading.Thread(target=pty_to_usb, daemon=True)
    t2 = threading.Thread(target=usb_to_pty, daemon=True)
    t1.start()
    t2.start()

    print("[3] 桥接已启动")
    print()
    print("=== 现在可以用这个端口跑 esptool ===")
    print("    export ESPTOOL_PORT=%s" % slave_name)
    print("    /root/rknn_env/bin/python -m esptool --chip esp32s3 \\")
    print("        --port %s --before no_reset --after no_reset \\" % slave_name)
    print("        write_flash ...")
    print()
    print("（保持这个进程运行，Ctrl-C 结束）")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[退出]")
    finally:
        stop.set()
        time.sleep(0.2)
        try:
            os.close(master_fd)
            os.close(slave_fd)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
