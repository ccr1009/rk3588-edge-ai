#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deep_diag.py — ESP32 能打开 USB 但收不到回应时的深度诊断

现象（2026-11-06 23:11）：
    · lsusb 能看到 303a:1001（Bus 001 Device 004）
    · pyusb 能枚举、能打开 CDC Data 接口
    · 但发 I 命令 → 0 行回应
    · 2 秒被动监听也是 0 行

这说明 USB 描述符在（ESP32-S3 的 USB-Serial-JTAG 是芯片内置的，
即使 app 卡死描述符也还在），但**固件可能没在跑**。

可能原因，按顺序排查：
    1. USB 端点状态坏了（之前异常断开留下的）→ 重新配置 interface
    2. CDC 的 line coding / DTR-RTS 没设对 → 重设
    3. 固件卡死（app_main 挂了）→ 需要复位
    4. ESP32 在复位循环（不断重启）→ 被动监听能看到重复的 BOOT 行

跑法：
    /root/rknn_env/bin/python /root/esp32_gimbal/deep_diag.py
"""
import struct
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
import usb.core
import usb.util

VID, PID = 0x303A, 0x1001
CDC_SET_LINE_CODING = 0x20
CDC_SET_CONTROL_LINE_STATE = 0x22


def say(t=""):
    print(t)
    sys.stdout.flush()


def find_device():
    return usb.core.find(idVendor=VID, idProduct=PID)


def get_ifaces(dev):
    data_if = ctrl_if = None
    for cfg in dev:
        for i in cfg:
            if i.bInterfaceClass == 0x0A:
                data_if = i
            elif i.bInterfaceClass == 0x02:
                ctrl_if = i
    return data_if, ctrl_if


def get_endpoints(data_if):
    ep_in = ep_out = None
    for ep in data_if:
        if (ep.bmAttributes & 3) == 2:
            if ep.bEndpointAddress & 0x80:
                ep_in = ep
            else:
                ep_out = ep
    return ep_in, ep_out


def read_for(ep_in, dur, label=""):
    """被动读 dur 秒，返回收到的行"""
    lines = []
    buf = b""
    t0 = time.time()
    while time.time() - t0 < dur:
        try:
            d = ep_in.read(512, timeout=100)
            if len(d):
                buf += bytes(d)
                while b"\n" in buf:
                    ln, buf = buf.split(b"\n", 1)
                    v = ln.decode("utf-8", "replace").strip()
                    if v:
                        lines.append(v)
        except usb.core.USBError as e:
            if getattr(e, "errno", None) == 110 or "timeout" in str(e).lower():
                continue
            lines.append("<<USBError: %s>>" % e)
            break
    if buf.strip():
        lines.append(buf.decode("utf-8", "replace").strip() + " (无换行)")
    return lines


def main():
    say("=" * 70)
    say("ESP32 深度诊断 —— 为什么能开 USB 但收不到回应")
    say("=" * 70)

    # ---------- 步骤 1：设备在不在 ----------
    say()
    say("[1] 设备枚举")
    dev = find_device()
    if dev is None:
        say("    ❌ 找不到 303a:1001")
        return 1
    say("    ✅ Bus %s Addr %s  序列号 %s" % (dev.bus, dev.address, dev.serial_number))

    data_if, ctrl_if = get_ifaces(dev)
    if data_if is None:
        say("    ❌ 找不到 CDC Data 接口")
        return 1
    ep_in, ep_out = get_endpoints(data_if)
    say("    CDC Data iface=%d  IN=0x%02x OUT=0x%02x"
        % (data_if.bInterfaceNumber, ep_in.bEndpointAddress if ep_in else -1,
           ep_out.bEndpointAddress if ep_out else -1))

    # ---------- 步骤 2：被动监听 5 秒 ----------
    # 如果 ESP32 在复位循环，会反复打印 "BOOT gimbal v2"
    say()
    say("[2] 被动监听 5 秒（不发任何命令）")
    say("    如果在复位循环，这里会反复出现 BOOT / READY 行")
    lines = read_for(ep_in, 5.0)
    if lines:
        say("    收到 %d 行：" % len(lines))
        for x in lines[-12:]:
            say("      %s" % x)
        boot_cnt = sum(1 for x in lines if "BOOT" in x or "READY" in x)
        if boot_cnt >= 2:
            say()
            say("    ⚠️ 检测到 %d 次启动信息 → **ESP32 在复位循环！**" % boot_cnt)
            say("       → 通常是供电不足或固件 panic。先把舵机线全拔掉再试。")
    else:
        say("    (0 行 —— 固件没有主动输出，符合「卡死但 USB 描述符还在」)")

    # ---------- 步骤 3：重新配置 CDC ----------
    say()
    say("[3] 重新配置 CDC（line coding + DTR/RTS）")
    try:
        dev.set_configuration()
        say("    set_configuration() OK")
    except Exception as e:
        say("    set_configuration() 报错（可忽略）: %s" % e)

    ctrl_num = ctrl_if.bInterfaceNumber if ctrl_if else 0
    lc = struct.pack("<IBBB", 115200, 0, 0, 8)
    try:
        dev.ctrl_transfer(0x21, CDC_SET_LINE_CODING, 0, ctrl_num, lc)
        say("    SET_LINE_CODING OK (115200 8N1)")
    except Exception as e:
        say("    SET_LINE_CODING 失败: %s" % e)
    for state, name in [(0x03, "DTR+RTS 都拉高"), (0x00, "都拉低"), (0x01, "只拉 DTR")]:
        try:
            dev.ctrl_transfer(0x21, CDC_SET_CONTROL_LINE_STATE, state, ctrl_num, None)
            say("    SET_CONTROL_LINE_STATE=0x%02x (%s) OK" % (state, name))
            time.sleep(0.1)
            # 每次设完就试着发命令
            ep_out.write(b"I\n", timeout=500)
            r = read_for(ep_in, 1.0)
            if r:
                say("      → 收到回应: %r" % r)
                if any(x.startswith("GPIN") for x in r):
                    say()
                    say("    ✅✅ 这个 DTR/RTS 组合能工作！固件是活的。")
                    say("       0x%02x (interface=%d) 就是正确配置" % (state, ctrl_num))
                    usb.util.dispose_resources(dev)
                    return 0
        except Exception as e:
            say("      SET_CONTROL_LINE_STATE=0x%02x 失败: %s" % (state, e))

    # ---------- 步骤 4：清端点 + 重发 ----------
    say()
    say("[4] 清空端点缓冲后重发 I")
    try:
        # 反复读把残留清干净
        read_for(ep_in, 0.5)
        try:
            ep_in.clear_halt()
        except Exception:
            pass
        try:
            ep_out.clear_halt()
        except Exception:
            pass
        ep_out.write(b"I\n", timeout=1000)
        say("    已发送 I")
        r = read_for(ep_in, 2.0)
        if r:
            say("    ✅ 收到: %r" % r)
        else:
            say("    ❌ 仍然 0 回应")
    except Exception as e:
        say("    写失败: %s" % e)

    # ---------- 步骤 5：结论 ----------
    say()
    say("=" * 70)
    say("结论")
    say("=" * 70)

    # 最后再确认一次设备号（如果变了说明在复位）
    dev2 = find_device()
    addr2 = dev2.address if dev2 is not None else None
    say("  设备地址: 开始时 %s → 现在 %s" % (dev.address, addr2))
    if addr2 is not None and addr2 != dev.address:
        say("  ⚠️ 地址变了 → ESP32 在这几十秒里重新枚举过，**在复位循环**")
        say("     → 拔掉所有舵机线，只留 USB，再跑一次")
        say("     → 如果还是复位，是供电问题（USB 口电流不够 or 板子问题）")

    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass

    say()
    say("  如果以上全部失败（4 种 DTR 组合都不回、监听也 0 行）：")
    say("     1. 先拔掉舵机的红/棕/橙三根线（排除舵机电流干扰）")
    say("     2. 重新插拔 ESP32 的 USB 线（插紧）")
    say("     3. 跑 /root/rknn_env/bin/python /root/esp32_gimbal/check_link.py")
    say("     4. 还不行就复位芯片：")
    say("        /root/rknn_env/bin/python /root/esp32_gimbal/esp32_reset.py")
    say("     5. 终极：在 Windows 上用 COM5 重新烧录（esptool）")
    say("=" * 70)
    return 1


if __name__ == "__main__":
    sys.exit(main())
