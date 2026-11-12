#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
esp32_usb_monitor.py — 持续监听 ESP32 的 CDC-ACM，自动重连，抓完整启动日志

为什么需要重连：
    每次 USB 复位后，dev 句柄会失效，端点需要重新打开。
    ESP32 复位（按 RST 或命令行触发）本身就会导致 CDC 重新枚举，
    所以监听必须能扛住"设备消失又出现"。

用法：
    /root/rknn_env/bin/python esp32_usb_monitor.py            # 一直监听
    /root/rknn_env/bin/python esp32_usb_monitor.py --secs 60   # 监听 60 秒
"""
import argparse
import struct
import sys
import time

try:
    import usb.core
    import usb.util
except ImportError:
    print("缺 pyusb", file=sys.stderr)
    sys.exit(2)

VID = 0x303A
PID = 0x1001

CDC_SET_LINE_CODING = 0x20
CDC_SET_CONTROL_LINE_STATE = 0x22
LINE_CODING = struct.pack("<IBBB", 115200, 0, 0, 8)


def open_cdc():
    """打开 ESP32 的 CDC-ACM，返回 (dev, ep_in, ep_out) 或 None"""
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        return None

    data_if = ctrl_if = None
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == 0x0A:      # CDC Data
                data_if = intf
            elif intf.bInterfaceClass == 0x02:    # CDC Communication
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
    if ep_in is None:
        return None

    # 分离内核驱动（如果占用了）
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

    ctrl_num = ctrl_if.bInterfaceNumber if ctrl_if else 0
    try:
        dev.ctrl_transfer(0x21, CDC_SET_LINE_CODING, 0, ctrl_num, LINE_CODING)
        dev.ctrl_transfer(0x21, CDC_SET_CONTROL_LINE_STATE, 0x03, ctrl_num, None)
    except Exception:
        pass

    return dev, ep_in, ep_out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=0, help="监听秒数（0=一直）")
    args = ap.parse_args()

    print("=" * 70)
    print("ESP32 USB-CDC 监听（用 pyusb 绕开缺失的 cdc_acm 驱动）")
    print("=" * 70)
    print()
    print("请按一下 ESP32 的 RST 键，或拔插它的 Type-C 触发启动日志")
    print()

    t_start = time.time()
    total = 0
    reconnects = 0
    handle = None
    last_open_fail = 0

    while True:
        now = time.time()
        if args.secs > 0 and now - t_start > args.secs:
            break

        # 确保连接
        if handle is None:
            h = open_cdc()
            if h is None:
                # 设备不在（可能在复位中），稍等重试
                if now - last_open_fail > 2.0:
                    print("  [%5.1fs] 设备未就绪，等待..." % (now - t_start))
                    last_open_fail = now
                time.sleep(0.3)
                continue
            handle = h
            reconnects += 1
            print("  [%5.1fs] ✅ CDC 已连接 (第 %d 次)"
                  % (now - t_start, reconnects))

        dev, ep_in, ep_out = handle
        try:
            data = ep_in.read(512, timeout=200)
            if len(data):
                b = bytes(data)
                total += len(b)
                p = sum(1 for c in b if 32 <= c < 127 or c in (9, 10, 13))
                ratio = p / len(b)
                tag = "文本" if ratio > 0.6 else "数据"
                txt = b.decode("utf-8", "replace").rstrip()
                print("  [%5.1fs] %s(%d): %s"
                      % (time.time() - t_start, tag, len(b), txt[:300]))
        except usb.core.USBError as e:
            # ★ 关键：Errno 110 = ETIMEDOUT = 读超时，表示"这段时间没有数据"，
            #   是**正常情况**，绝不能当成设备掉线去重连——
            #   重连会丢掉端点状态，反而永远收不到数据。
            #   第一版就是没区分这个，导致 69 次疯狂重连、零字节。
            errno_v = getattr(e, "errno", None)
            msg = str(e).lower()
            if errno_v == 110 or "timed out" in msg or "timeout" in msg:
                continue        # 只是没数据，继续读

            print("  [%5.1fs] USB 错误: %s —— 重连"
                  % (time.time() - t_start, e))
            try:
                usb.util.dispose_resources(dev)
            except Exception:
                pass
            handle = None
            time.sleep(0.5)
        except Exception as e:
            print("  [%5.1fs] 异常: %s" % (time.time() - t_start, e))
            handle = None
            time.sleep(0.3)

    print()
    print("=" * 70)
    print("监听结束：共 %d 字节，重连 %d 次" % (total, reconnects))
    if total == 0:
        print()
        print("❌ 零字节。可能原因：")
        print("  1) ESP32 的 CDC 只输出到 UART0，没输出到 USB")
        print("  2) 固件的 USB 串口驱动没初始化")
        print("  3) 需要先发数据触发")
    else:
        print("✅ 收到了数据 —— USB 通路可用")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
