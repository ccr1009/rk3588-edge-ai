#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
esp32_usb_cdc.py — 用 pyusb 直接驱动 ESP32 的 USB-JTAG CDC-ACM 接口

为什么需要这个：
    EVM3588-B 内核没编 CONFIG_USB_ACM（实测 `# CONFIG_USB_ACM is not set`），
    也没有 cdc_acm.ko 模块文件，所以 ESP32 的 CDC-ACM 接口建不出 /dev/ttyACM0。

    但内核里明明有 cdc_ether / cdc_ncm / cdc_wdm / cdc_subset —— 说明 CDC 框架在，
    只是少了 ACM 这个"上层消费者"。

    既然不能走内核，就在用户态用 libusb 直接跟 CDC-ACM 通信。

CDC-ACM 协议要点（自己实现要知道的）：
    ① 找 interface class=02 subclass=02（ACM Data）
      和 class=0a（CDC 控制）
    ② 用 class-specific SET_LINE_CODING 设置波特率（8N1）
    ③ 用 SET_CONTROL_LINE_STATE 拉 DTR/RTS（很多设备不拉就不发数据）
    ④ 数据收发走 Data 接口的 bulk IN/OUT 端点

ESP32-S3 的接口布局（实测）：
    1-1.2:1.0  class=02 sub=02  → ACM Data（bulk in/out）
    1-1.2:1.1  class=0a sub=02  → CDC 控制
    1-1.2:1.2  class=ff sub=ff  → vendor specific（JTAG，不用）

用法：
    sudo python3 esp32_usb_cdc.py                 # 交互测试
    sudo python3 esp32_usb_cdc.py --listen 20      # 监听 20 秒
"""
import argparse
import struct
import sys
import time

try:
    import usb.core
    import usb.util
except ImportError:
    print("缺 pyusb：/root/rknn_env/bin/python -m pip install pyusb", file=sys.stderr)
    sys.exit(2)

VID = 0x303A
PID = 0x1001

# CDC class-specific requests (ACM)
CDC_SET_LINE_CODING = 0x20
CDC_GET_LINE_CODING = 0x21
CDC_SET_CONTROL_LINE_STATE = 0x22

# line coding: (dwDTERate, bCharFormat, bParityType, bDataBits)
LINE_CODING = struct.pack("<IBBB", 115200, 0, 0, 8)   # 115200 8N1


def find_device():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        dev = usb.core.find(idVendor=VID, idProduct=PID, find_all=False)
    return dev


def describe(dev):
    print("=== ESP32 USB 描述符 ===")
    print("  VID:PID = %04x:%04x" % (dev.idVendor, dev.idProduct))
    try:
        print("  制造商  = %s" % dev.manufacturer)
        print("  产品    = %s" % dev.product)
        print("  序列号  = %s" % dev.serial_number)
    except Exception:
        pass
    print()
    for cfg in dev:
        print("  配置 %d: %d 个接口" % (cfg.bConfigurationValue, cfg.bNumInterfaces))
        for intf in cfg:
            cls = intf.bInterfaceClass
            sub = intf.bInterfaceSubClass
            proto = intf.bInterfaceProtocol
            print("    接口 %d.%d  class=%02x sub=%02x proto=%02x  端点数=%d"
                  % (cfg.bConfigurationValue, intf.bInterfaceNumber,
                     cls, sub, proto, intf.bNumEndpoints))
            for ep in intf:
                dir_s = "IN" if ep.bEndpointAddress & 0x80 else "OUT"
                print("      %s  0x%02x  %s  maxpacket=%d"
                      % (dir_s, ep.bEndpointAddress,
                         ["control", "iso", "bulk", "intr"][ep.bmAttributes & 3],
                         ep.wMaxPacketSize))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=float, default=0, help="监听秒数（0=交互）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    dev = find_device()
    if dev is None:
        print("找不到 ESP32 (303a:1001)。确认 Type-C 插在 RK3588 上。",
              file=sys.stderr)
        return 1

    if args.verbose:
        describe(dev)
        print()

    # 找 ACM Data 接口
    #
    # ★ 特别注意（2026-11-04 实测踩的坑）：
    #   CDC 规范里 class 编号容易记反：
    #     class=0x02 (CDC Communication) → 控制接口，端点是 **interrupt**
    #     class=0x0A (CDC Data)          → 数据接口，端点是 **bulk**
    #   ESP32-S3 实测布局：
    #     接口 0  class=02 sub=02  1 个 intr IN (0x82)   ← 通知
    #     接口 1  class=0a sub=02  bulk OUT(0x01)/IN(0x81) ← 数据
    #   第一版我在 class=02 里找 bulk 端点，结果"找不到 bulk 端点"。
    acm_if = None      # class=0a 数据接口
    cdc_if = None      # class=02 控制接口
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == 0x0A:      # CDC Data
                acm_if = intf
            elif intf.bInterfaceClass == 0x02:    # CDC Communication
                cdc_if = intf

    if acm_if is None:
        print("没找到 ACM Data 接口 (class=02 sub=02)", file=sys.stderr)
        describe(dev)
        return 1

    print("找到 ACM Data 接口: %d" % acm_if.bInterfaceNumber)
    if cdc_if:
        print("找到 CDC 控制接口: %d" % cdc_if.bInterfaceNumber)

    # 分离内核驱动（如果被占用）
    for intf_num in (acm_if.bInterfaceNumber,
                     cdc_if.bInterfaceNumber if cdc_if else -1):
        if intf_num < 0:
            continue
        try:
            if dev.is_kernel_driver_active(intf_num):
                dev.detach_kernel_driver(intf_num)
                print("已分离内核驱动 interface %d" % intf_num)
        except Exception as e:
            if args.verbose:
                print("detach 跳过: %s" % e)

    try:
        dev.set_configuration()
    except Exception as e:
        print("set_configuration 失败: %s" % e, file=sys.stderr)

    # 找 bulk 端点
    ep_in = ep_out = None
    for ep in acm_if:
        if (ep.bmAttributes & 3) == 2:      # bulk
            if ep.bEndpointAddress & 0x80:
                ep_in = ep
            else:
                ep_out = ep

    if ep_in is None or ep_out is None:
        print("找不到 bulk 端点", file=sys.stderr)
        describe(dev)
        return 1

    print("bulk IN = 0x%02x   bulk OUT = 0x%02x"
          % (ep_in.bEndpointAddress, ep_out.bEndpointAddress))

    # 设置 line coding（115200 8N1）
    ctrl_if = cdc_if.bInterfaceNumber if cdc_if else 0
    try:
        dev.ctrl_transfer(0x21, CDC_SET_LINE_CODING, 0, ctrl_if, LINE_CODING)
        print("已设置 line coding: 115200 8N1")
    except Exception as e:
        print("SET_LINE_CODING 失败: %s" % e, file=sys.stderr)

    # 拉 DTR/RTS
    try:
        dev.ctrl_transfer(0x21, CDC_SET_CONTROL_LINE_STATE, 0x03, ctrl_if, None)
        print("已拉 DTR+RTS")
    except Exception as e:
        print("SET_CONTROL_LINE_STATE 失败: %s" % e, file=sys.stderr)

    time.sleep(0.3)

    if args.listen > 0:
        # 监听模式
        print()
        print("监听 %.0f 秒 ...（请按 ESP32 的 RST）" % args.listen)
        t0 = time.time()
        total = 0
        while time.time() - t0 < args.listen:
            try:
                data = ep_in.read(512, timeout=200)
                if data:
                    total += len(data)
                    b = bytes(data)
                    p = sum(1 for c in b if 32 <= c < 127 or c in (9, 10, 13))
                    tag = "文本" if (len(b) and p / len(b) > 0.6) else "数据"
                    print("  [%5.1fs] %s(%d): %s"
                          % (time.time() - t0, tag, len(b),
                             b.decode("utf-8", "replace").rstrip()[:200]))
            except Exception:
                pass
        print()
        print("共收到 %d 字节" % total)
        if total == 0:
            print("❌ 零字节 —— 可能是：")
            print("   a) ESP32 的 CDC 接口没数据（固件没把日志输出到 USB）")
            print("   b) 需要先发数据触发")
            print("   c) 端点方向/接口选错")
    else:
        # 交互模式
        print()
        print("交互模式，输入命令（Q/I/P 0 0/H），空行退出")
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            try:
                ep_out.write((line + "\n").encode(), timeout=1000)
                print("  已发送: %r" % line)
            except Exception as e:
                print("  发送失败: %s" % e)
            # 读回复
            t0 = time.time()
            got = 0
            while time.time() - t0 < 1.0:
                try:
                    data = ep_in.read(512, timeout=150)
                    if data:
                        got += len(data)
                        b = bytes(data)
                        print("  <-- %s" % b.decode("utf-8", "replace").rstrip())
                except Exception:
                    pass
            if got == 0:
                print("  <-- (无回复)")

    # 清理
    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
