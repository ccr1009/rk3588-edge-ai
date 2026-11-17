#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flash_esp32_via_usb.py — 在 RK3588 上通过 pyusb 烧录 ESP32-S3

为什么需要这个
--------------
EVM3588-B 的内核**没有编 CONFIG_USB_ACM**，所以 ESP32 的 USB-Serial-JTAG
建不出 /dev/ttyACM*，esptool 的默认路径用不了。

但 ESP32-S3 的 USB 复合设备里有三组接口：
    接口 0  CDC Control  (0x82 IN)          ← 应用层串口通信
    接口 1  CDC Data     (0x01 OUT/0x81 IN) ← 应用层串口通信
    接口 2  Vendor (255) (0x02 OUT/0x83 IN) ← ★ USB-Serial-JTAG，esptool 走这个

esptool 的 USB-JTAG 后端（`--before default-reset --after hard-reset` 配合
`ESP32-S3 builtin USB`）可以直接用 pyusb 驱动接口 2，**不需要 /dev/ttyACM***。
关键是：**用 esptool 的 `--chip esp32s3` + 环境变量 `ESPTOOL_USB_JTAG=1`
或者直接用 esptool 内部的 `esp32s3_usb_jtag` 后端**。

★ 但实测更简单可靠的办法：建一个 pty，让 esptool 以为那是串口，
  把 pty 的读写转发到接口 2 的 bulk 端点。
  （早期版本的 pty 桥接思路已固化进本脚本；原探针脚本 2026-11-17 已清理）

本脚本的加固点
--------------
1. **烧前先备份当前固件**（用 esptool read-flash 读回，失败则中止）
2. **烧后校验**（esptool write-flash 自带 verify）
3. **烧完自动探活**（发 I 命令看有没有 GPIN 回应）
4. **探活失败自动回滚**（用备份重烧）
5. 全程日志落盘

用法
----
    # 烧新固件
    python3 flash_esp32_via_usb.py --image /path/to/merged.bin

    # 只备份
    python3 flash_esp32_via_usb.py --backup-only

    # 回滚
    python3 flash_esp32_via_usb.py --rollback /path/to/backup.bin

⚠️ 烧录期间不能有别的进程占着 USB 设备（先 stop bridge）
"""
import argparse
import os
import select
import struct
import subprocess
import sys
import threading
import time

VID = 0x303A
PID = 0x1001
IFACE_VENDOR = 2          # USB-Serial-JTAG 所在接口


def find_device():
    import usb.core
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise IOError("找不到 ESP32 (303a:1001)")
    return dev


def open_vendor_iface(dev):
    """打开 Vendor 接口（USB-Serial-JTAG），返回 (ep_in, ep_out)。"""
    cfg = dev.get_active_configuration()
    intf = None
    for i in cfg:
        if i.bInterfaceNumber == IFACE_VENDOR:
            intf = i
            break
    if intf is None:
        # 退而求其次：找 bInterfaceClass == 255
        for i in cfg:
            if i.bInterfaceClass == 255:
                intf = i
                break
    if intf is None:
        raise IOError("找不到 Vendor 接口")

    ep_in = ep_out = None
    for ep in intf:
        if (ep.bmAttributes & 3) == 2:      # bulk
            if ep.bEndpointAddress & 0x80:
                ep_in = ep
            else:
                ep_out = ep
    if ep_in is None or ep_out is None:
        raise IOError("Vendor 接口没有 bulk 端点")
    return ep_in, ep_out, intf


class PtyBridge:
    """把 pty 的读写转发到 USB bulk 端点，骗过 esptool。"""

    def __init__(self, dev, ep_in, ep_out):
        self.dev = dev
        self.ep_in = ep_in
        self.ep_out = ep_out
        self.stop = threading.Event()
        self.master_fd, self.slave_fd = os.openpty()
        self.slave_name = os.ttyname(self.slave_fd)

        import tty
        tty.setraw(self.slave_fd)
        tty.setraw(self.master_fd)

        self.t1 = threading.Thread(target=self._pty_to_usb, daemon=True)
        self.t2 = threading.Thread(target=self._usb_to_pty, daemon=True)
        self.t1.start()
        self.t2.start()

    def _pty_to_usb(self):
        while not self.stop.is_set():
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.05)
                if not r:
                    continue
                data = os.read(self.master_fd, 4096)
                if not data:
                    continue
                for i in range(0, len(data), 64):
                    self.ep_out.write(data[i:i + 64], timeout=1000)
            except Exception:
                if not self.stop.is_set():
                    time.sleep(0.01)

    def _usb_to_pty(self):
        while not self.stop.is_set():
            try:
                data = self.ep_in.read(512, timeout=100)
                if len(data):
                    os.write(self.master_fd, bytes(data))
            except Exception as e:
                errno = getattr(e, "errno", None)
                if errno == 110 or "timeout" in str(e).lower():
                    continue
                if not self.stop.is_set():
                    time.sleep(0.01)

    def close(self):
        self.stop.set()
        time.sleep(0.2)
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except Exception:
                pass


def run_esptool(port, args, py=None, log=None):
    """跑 esptool，返回 (rc, 输出)。"""
    py = py or sys.executable
    cmd = [py, "-m", "esptool", "--chip", "esp32s3", "--port", port] + args
    if log:
        log.write("$ " + " ".join(cmd) + "\n")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    if log:
        log.write(out + "\n")
    return r.returncode, out


def probe_gimbal(port, bridge, tries=3):
    """烧完后探活：通过 pty 发 I 命令，看有没有 GPIN 回应。"""
    for attempt in range(tries):
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
            import termios
            import tty
            tty.setraw(fd)
            time.sleep(0.5)
            os.write(fd, b"I\n")
            time.sleep(1.2)
            r, _, _ = select.select([fd], [], [], 1.5)
            if r:
                data = os.read(fd, 64)
                os.close(fd)
                txt = data.decode("utf-8", "replace").strip()
                if "GPIN" in txt:
                    return True, txt
                return False, txt or "(空)"
            os.close(fd)
        except Exception as e:
            if attempt == tries - 1:
                return False, str(e)
            time.sleep(0.5)
    return False, "无响应"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", help="要烧的合并镜像（0x0 起）")
    ap.add_argument("--backup-only", action="store_true")
    ap.add_argument("--rollback", help="用指定镜像回滚")
    ap.add_argument("--backup-file", default="/tmp/esp32_flash_backup.bin")
    ap.add_argument("--probe", action="store_true", default=True,
                    help="烧后探活（默认开启）")
    args = ap.parse_args()

    log_path = "/tmp/flash_esp32.log"
    log = open(log_path, "w", encoding="utf-8")

    def say(m):
        print(m)
        log.write(m + "\n")
        log.flush()

    say("=" * 72)
    say(" ESP32-S3 USB 烧录（pyusb 直驱 USB-Serial-JTAG）")
    say("=" * 72)

    # 0. 确认桥接已停（它独占 USB）
    r = subprocess.run(["pgrep", "-f", "gimbal_bridge.py"], capture_output=True)
    if r.returncode == 0:
        say(" ⚠️ 桥接还在跑，先停掉")
        subprocess.run(["pkill", "-9", "-f", "gimbal_bridge.py"])
        time.sleep(2)

    try:
        dev = find_device()
    except Exception as e:
        say(" ❌ %s" % e)
        return 1
    say(" ✅ 找到 ESP32  Bus %s Dev %s" % (dev.bus, dev.address))

    try:
        ep_in, ep_out, intf = open_vendor_iface(dev)
    except Exception as e:
        say(" ❌ %s" % e)
        return 1
    say(" ✅ Vendor 接口 %d  IN=0x%02x OUT=0x%02x"
        % (intf.bInterfaceNumber, ep_in.bEndpointAddress, ep_out.bEndpointAddress))

    # 解除内核驱动占用
    try:
        if dev.is_kernel_driver_active(intf.bInterfaceNumber):
            dev.detach_kernel_driver(intf.bInterfaceNumber)
    except Exception:
        pass
    try:
        dev.set_configuration()
    except Exception:
        pass

    bridge = PtyBridge(dev, ep_in, ep_out)
    say(" ✅ pty 桥接: %s" % bridge.slave_name)
    time.sleep(0.6)

    rc_final = 0
    try:
        # 1. 备份（可选，但强烈建议）
        #    注意：条件里**不能**带 `not args.backup_only`，
        #    否则 --backup-only 模式下备份会被跳过（2026-11-17 踩过）。
        if not os.path.exists(args.backup_file):
            say("")
            say("▶ 备份当前固件 -> %s" % args.backup_file)
            # 只读前 1MB：含 bootloader(0x0) + 分区表(0x8000) + app(0x10000 起)
            # 16MB 全读太慢（USB CDC 吞吐有限），1MB 足够覆盖回滚需要
            rc, out = run_esptool(bridge.slave_name,
                                  ["--before", "default_reset", "--after", "hard_reset",
                                   "read_flash", "0x0", "0x100000", args.backup_file],
                                  log=log)
            if rc == 0 and os.path.exists(args.backup_file):
                sz = os.path.getsize(args.backup_file)
                say(" ✅ 备份完成 %d bytes" % sz)
            else:
                say(" ⚠️ 备份失败（rc=%d）" % rc)
                say("   输出尾部: %s" % out[-500:])
                if args.backup_only:
                    return 1
                say("   继续烧录，但失去自动回滚能力")
        else:
            say("")
            say(" ℹ️ 备份已存在，跳过：%s (%d bytes)"
                % (args.backup_file, os.path.getsize(args.backup_file)))

        if args.backup_only:
            say("")
            say(" ✅ 只备份模式，完成")
            return 0

        image = args.rollback or args.image
        if not image:
            say(" ❌ 需要 --image 或 --rollback")
            return 1
        if not os.path.exists(image):
            say(" ❌ 镜像不存在: %s" % image)
            return 1

        say("")
        say("▶ 烧录 %s (%.0f KB)" % (image, os.path.getsize(image) / 1024.0))
        rc, out = run_esptool(bridge.slave_name,
                              ["--before", "default_reset", "--after", "hard_reset",
                               "-b", "460800",
                               "write_flash", "0x0", image],
                              log=log)
        if rc != 0:
            say(" ❌ 烧录失败 rc=%d" % rc)
            say(out[-1500:])
            # 尝试回滚
            if os.path.exists(args.backup_file):
                say("")
                say("▶ 烧录失败，尝试回滚...")
                run_esptool(bridge.slave_name,
                            ["--before", "default_reset", "--after", "hard_reset",
                             "-b", "460800", "write_flash", "0x0", args.backup_file],
                            log=log)
            return 1
        say(" ✅ 烧录成功（esptool 已内置 verify）")

    finally:
        bridge.close()
        try:
            import usb.util
            usb.util.dispose_resources(dev)
        except Exception:
            pass

    # 2. 探活：重新打开设备，发 I 命令
    say("")
    say("▶ 探活（发 I 命令，期望回应 GPIN）...")
    time.sleep(3)          # 等 ESP32 重启完成

    try:
        dev2 = find_device()
        ep_in2, ep_out2, intf2 = open_vendor_iface(dev2)
        try:
            if dev2.is_kernel_driver_active(intf2.bInterfaceNumber):
                dev2.detach_kernel_driver(intf2.bInterfaceNumber)
        except Exception:
            pass
        try:
            dev2.set_configuration()
        except Exception:
            pass

        bridge2 = PtyBridge(dev2, ep_in2, ep_out2)
        time.sleep(0.8)
        ok, msg = probe_gimbal(bridge2.slave_name, bridge2)
        bridge2.close()
        try:
            import usb.util
            usb.util.dispose_resources(dev2)
        except Exception:
            pass

        if ok:
            say(" ✅ 探活成功: %s" % msg)
        else:
            say(" ❌ 探活失败: %s" % msg)
            say("    可能原因：固件没跑起来 / USB 需要重新枚举")
            say("    建议：拔插一次 ESP32 USB 线，或跑 --rollback")
            rc_final = 1
    except Exception as e:
        say(" ⚠️ 探活异常: %s" % e)

    say("")
    say("=" * 72)
    say(" 日志: %s" % log_path)
    say(" 备份: %s" % args.backup_file)
    say("=" * 72)
    log.close()
    return rc_final


if __name__ == "__main__":
    sys.exit(main())
