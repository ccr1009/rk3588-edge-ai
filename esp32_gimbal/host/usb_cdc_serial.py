#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usb_cdc_serial.py — 用 pyusb 实现一个"像 pyserial"的串口对象

背景（2026-11-04）：
    EVM3588-B 内核没编 CONFIG_USB_ACM，ESP32 的 USB-JTAG CDC 接口
    建不出 /dev/ttyACM0，所以 pyserial 用不了。

    已实测验证：用 pyusb 直接操作 CDC-ACM 端点，收发完全正常：
        --> 'I'  <-- GPIN 14 15
        --> 'Q'  <-- S 0.0 0.0 1

    这个文件把 pyusb 包装成 Serial 的子类，让现有代码（gimbal_tracker.py、
    gimbal_probe.py 等）**一行不改**就能用 USB 通路。

用法：
    from usb_cdc_serial import UsbCdcSerial
    ser = UsbCdcSerial()         # 不用传端口名
    ser.write(b"Q\n")
    line = ser.readline()

设计要点：
    ① readline 必须自己实现（pyusb 只有 bulk read，没有行缓冲）
    ② 超时用 errno 110 (ETIMEDOUT) 判断，不能当错误
    ③ 关闭时释放 USB 资源，否则下次打不开
"""
import struct
import sys
import time

try:
    import usb.core
    import usb.util
except ImportError:
    print("缺 pyusb：pip install pyusb", file=sys.stderr)
    raise

VID = 0x303A
PID = 0x1001

CDC_SET_LINE_CODING = 0x20
CDC_SET_CONTROL_LINE_STATE = 0x22


class UsbCdcSerial:
    """最小化的串口兼容类：write / read / readline / reset_input_buffer / close"""

    def __init__(self, baudrate=115200, timeout=0.5, vid=VID, pid=PID):
        self.baudrate = baudrate
        self.timeout = timeout
        self._buf = b""
        self._dev = None
        self._ep_in = None
        self._ep_out = None
        self._line_state = None       # 实际生效的 DTR/RTS 组合
        self.port = "usb:303a:1001"
        self._open(vid, pid)

    # ---------------- 打开 / 关闭 ----------------
    def _open(self, vid, pid):
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise IOError("找不到 ESP32 USB 设备 (%04x:%04x)" % (vid, pid))

        data_if = ctrl_if = None
        for cfg in dev:
            for intf in cfg:
                # ★ class=0A 才是 CDC Data（有 bulk 端点），class=02 是控制接口
                if intf.bInterfaceClass == 0x0A:
                    data_if = intf
                elif intf.bInterfaceClass == 0x02:
                    ctrl_if = intf
        if data_if is None:
            raise IOError("找不到 CDC Data 接口 (class=0x0a)")

        for ep in data_if:
            if (ep.bmAttributes & 3) == 2:          # bulk
                if ep.bEndpointAddress & 0x80:
                    self._ep_in = ep
                else:
                    self._ep_out = ep
        if self._ep_in is None:
            raise IOError("找不到 bulk IN 端点")

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
        # line coding: dwDTERate, bCharFormat(0=1stop), bParityType(0=none), bDataBits(8)
        lc = struct.pack("<IBBB", self.baudrate, 0, 0, 8)
        try:
            dev.ctrl_transfer(0x21, CDC_SET_LINE_CODING, 0, ctrl_num, lc)
        except Exception:
            pass

        # ★★★ DTR/RTS 的状态决定 ESP32 能不能工作（2026-11-06 实测定位）★★★
        #
        # 这里踩过一个很隐蔽的坑：原来无脑拉高 DTR+RTS(0x03)，理由是
        # "很多 CDC 设备不拉这两根线就不发数据"。结果 11-06 那天出现：
        #     · lsusb 能看到 303a:1001，pyusb 也能打开 CDC
        #     · 但发 I 命令 → 0 行回应，被动监听 5 秒也是 0 行
        #     · 看起来像"固件卡死"，差点去重烧固件
        #
        # 逐组试过 0x03 / 0x00 / 0x01 之后，真正能通的是 **0x00（都拉低）**：
        #     0x03 → 无回应
        #     0x00 → 'GPIN 14 15'   ✅
        #
        # 原理：ESP32-S3 的 USB-Serial-JTAG 把 DTR/RTS 电平解释成
        # **自动下载电路的复位/启动模式信号**（等价于 USB-TTL 模块上那对
        # 经典的 auto-reset 三极管：DTR+RTS 组合会拉 EN / IO0）。
        # 拉高 0x03 等于持续按住复位/进下载模式 → 芯片反复重启，
        # app_main 永远跑不到处理命令的那一步。
        # 而 USB 描述符是芯片内置 ROM 提供的，**即使 app 没跑也照样在**，
        # 所以"能枚举、能打开、但不回话"。
        #
        # 这条不能用"能打开就算成功"来判 —— 打开成功和能通是完全两件事。
        self._set_line_state(dev, ctrl_num, 0x00)

        self._dev = dev
        time.sleep(0.1)
        self.reset_input_buffer()

        # 打开后做一次探活：连不上说明 DTR/RTS 组合不对，自动换一种重试。
        # 这样不管换哪块板子、哪种 USB-Serial-JTAG 固件版本都不会卡死。
        if not self._probe_alive():
            for state in (0x03, 0x01, 0x02):
                try:
                    self._set_line_state(dev, ctrl_num, state)
                    time.sleep(0.15)
                    self.reset_input_buffer()
                    if self._probe_alive():
                        self._line_state = state
                        break
                except Exception:
                    continue

    def _set_line_state(self, dev, ctrl_num, state):
        """设置 DTR/RTS。失败不抛（有些平台不支持这个请求）"""
        try:
            dev.ctrl_transfer(0x21, CDC_SET_CONTROL_LINE_STATE, state,
                              ctrl_num, None)
            self._line_state = state
        except Exception:
            pass

    def _probe_alive(self):
        """发一条 I，看有没有 GPIN 回应。用最短超时，只用于探活"""
        try:
            self._ep_out.write(b"I\n", timeout=500)
            deadline = time.time() + 1.0
            buf = b""
            while time.time() < deadline:
                try:
                    d = self._ep_in.read(512, timeout=100)
                except usb.core.USBError as e:
                    if getattr(e, "errno", None) == 110 or "timeout" in str(e).lower():
                        continue
                    return False
                if len(d):
                    buf += bytes(d)
                    if b"GPIN" in buf or b"OK" in buf or b"S " in buf:
                        self._buf = b""      # 探活的回包丢掉，别污染后续读
                        return True
            return False
        except Exception:
            return False

    def close(self):
        if self._dev is not None:
            try:
                usb.util.dispose_resources(self._dev)
            except Exception:
                pass
        self._dev = None
        self._ep_in = None
        self._ep_out = None
        self._buf = b""

    # ---------------- 兼容 pyserial 的属性 ----------------
    @property
    def is_open(self):
        return self._dev is not None and self._ep_in is not None

    # ---------------- 读写 ----------------
    def write(self, data):
        if not self.is_open:
            raise IOError("串口未打开")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("write 需要 bytes")
        total = 0
        payload = bytes(data)
        # USB bulk 单次最多 64 字节（maxpacket），分块写
        for i in range(0, len(payload), 64):
            chunk = payload[i:i + 64]
            try:
                n = self._ep_out.write(chunk, timeout=1000)
                total += n
            except Exception as e:
                raise IOError("USB 写失败: %s" % e)
        return total

    def flush(self):
        # bulk 写是同步的，无需额外操作
        pass

    def _read_chunk(self, wait=None):
        """读一次 USB，返回 bytes（可能为空）"""
        if not self.is_open:
            raise IOError("串口未打开")
        w = int((wait if wait is not None else self.timeout) * 1000)
        if w <= 0:
            w = 1
        try:
            d = self._ep_in.read(512, timeout=w)
            return bytes(d) if len(d) else b""
        except usb.core.USBError as e:
            # errno 110 = 超时，正常情况，返回空
            if getattr(e, "errno", None) == 110 or "timeout" in str(e).lower():
                return b""
            raise IOError("USB 读失败: %s" % e)

    def read(self, size=1):
        """读最多 size 字节，超时返回已有的"""
        deadline = time.time() + self.timeout
        while len(self._buf) < size and time.time() < deadline:
            chunk = self._read_chunk(wait=min(0.05, self.timeout))
            if chunk:
                self._buf += chunk
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    def readline(self):
        """读到 \\n 为止（含 \\n），超时返回已有内容"""
        deadline = time.time() + self.timeout
        while b"\n" not in self._buf and time.time() < deadline:
            chunk = self._read_chunk(wait=0.05)
            if chunk:
                self._buf += chunk
        idx = self._buf.find(b"\n")
        if idx >= 0:
            out, self._buf = self._buf[:idx + 1], self._buf[idx + 1:]
            return out
        # 超时且没换行：返回已缓冲的内容（可能为空）
        out, self._buf = self._buf, b""
        return out

    def reset_input_buffer(self):
        """清空输入缓冲（连续读到超时为止）"""
        self._buf = b""
        t0 = time.time()
        while time.time() - t0 < 0.3:
            chunk = self._read_chunk(wait=0.03)
            if not chunk:
                break

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ---------------- 自测 ----------------
if __name__ == "__main__":
    print("=" * 64)
    print("usb_cdc_serial 自测")
    print("=" * 64)
    try:
        ser = UsbCdcSerial(timeout=0.6)
    except Exception as e:
        print("打不开: %s" % e)
        sys.exit(1)

    print("已连接:", ser.port)
    for cmd in ["Q", "I", "P 0 0", "Q"]:
        ser.write((cmd + "\n").encode())
        ser.flush()
        line = ser.readline().decode("utf-8", "replace").strip()
        print("  %-8s -> %r" % (cmd, line))

    ser.close()
    print()
    print("✅ 自测完成")
