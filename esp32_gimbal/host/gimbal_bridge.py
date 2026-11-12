#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gimbal_bridge.py — 云台桥接服务

作用：
    监听一个本地 TCP 端口，收到 "PAN TILT\n" 就通过 USB CDC 转给 ESP32。
    解决 C++ 程序没有 libusb 头文件、不能直接控 USB 的问题。

为什么用 TCP 而不是 Unix socket：
    C++ 侧加 socket 代码最少（几个系统调用），Python 侧也简单。
    Linux 上 localhost TCP 延迟 <0.1ms，对 20~30Hz 控制完全够。

协议（极简）：
    "P <pan> <tilt>\n"   设置角度
    "H\n"               回中
    "Q\n"               查询（回复 S pan tilt ready）
    "STAT\n"            桥自己的统计（不转发）
    "RAW <任何一行>\n"   原样转发一行给 ESP32，回一行（诊断命令走这条）
    <其它任何单行>       也原样转发给 ESP32 并回一行 —— 见下方说明

★★★ 为什么默认改成「直通」（2026-11-05）★★★
    早期版本只认 P/H/Q/STAT 四条，其余一律回 "ERR unknown_cmd"。
    结果排查舵机时发 I / R 14 / V 14 / G 14 / T 15 全被桥吃掉，
    误判成「固件没烧进去 / 不是 v2 固件」，白折腾半天。

    固件自己的未知命令回的是 "ERR 9 unknown_cmd"（带数字 9，见 send_err(9,...)），
    桥回的是 "ERR unknown_cmd"（不带数字）。看到不带数字的就知道是桥挡了。
    现在默认全量直通，这个坑不会再犯。

用法：
    python3 gimbal_bridge.py                  # 监听 127.0.0.1:9100
    python3 gimbal_bridge.py --port 9100
    python3 gimbal_bridge.py --strict         # 恢复旧行为：只放行 P/H/Q/STAT
"""
import argparse
import socket
import sys
import threading
import time

sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial

# 串口是独占资源，多线程访问必须加锁
_usb_lock = threading.Lock()

# 由 --strict 置为 True：只放行 P/H/Q/STAT，其它一律本地回 ERR unknown_cmd（旧行为）
STRICT = False


class GimbalUSB:
    def __init__(self):
        self.ser = None
        self.pan = 0.0
        self.tilt = 0.0
        self.sent = 0
        self.errors = 0
        self._connect()

    def _connect(self):
        try:
            self.ser = UsbCdcSerial(timeout=0.4)
            print("[bridge] USB CDC 已连接")
            # 回中，确保起点已知
            self._send_raw("H\n")
            time.sleep(0.3)
            self.ser.reset_input_buffer()
            return True
        except Exception as e:
            print("[bridge] USB 连接失败: %s" % e, file=sys.stderr)
            self.ser = None
            return False

    def _send_raw(self, line):
        if self.ser is None:
            if not self._connect():
                return None
        try:
            self.ser.write(line.encode() if isinstance(line, str) else line)
            self.ser.flush()
            deadline = time.time() + 0.4
            while time.time() < deadline:
                raw = self.ser.readline()
                if not raw:
                    continue
                resp = raw.decode("utf-8", "replace").strip()
                if resp:
                    return resp
            return None
        except Exception as e:
            print("[bridge] 写失败: %s（重连）" % e)
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            self.errors += 1
            return None

    def set_angles(self, pan, tilt):
        """返回 (成功, 实际角度说明)"""
        with _usb_lock:
            resp = self._send_raw("P %.2f %.2f\n" % (pan, tilt))
        if resp is None:
            return False, "no_resp"
        if resp.startswith("OK"):
            parts = resp.split()
            if len(parts) >= 3:
                try:
                    self.pan = float(parts[1])
                    self.tilt = float(parts[2])
                except ValueError:
                    pass
            self.sent += 1
            return True, resp
        if "out_of_range" in resp:
            # 固件已夹紧执行，不算致命
            self.pan, self.tilt = pan, tilt
            self.sent += 1
            return True, resp
        self.errors += 1
        return False, resp

    def center(self):
        with _usb_lock:
            return self._send_raw("H\n")

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass


def handle_client(conn, addr, gimbal):
    """处理一个 C++ 客户端连接"""
    print("[bridge] 客户端接入 %s:%d" % addr)
    fx = conn.makefile("rwb", buffering=0)
    try:
        while True:
            line = fx.readline()
            if not line:
                break
            cmd = line.decode("utf-8", "replace").strip()
            if not cmd:
                continue

            if cmd.startswith("P "):
                parts = cmd.split()
                if len(parts) >= 3:
                    try:
                        pan = float(parts[1])
                        tilt = float(parts[2])
                    except ValueError:
                        fx.write(b"ERR bad_number\n")
                        continue
                    ok, resp = gimbal.set_angles(pan, tilt)
                    fx.write(((resp or "ERR no_resp") + "\n").encode())
                else:
                    fx.write(b"ERR need_two_args\n")
            elif cmd == "H":
                r = gimbal.center()
                fx.write(((r or "ERR") + "\n").encode())
            elif cmd == "Q":
                r = None
                with _usb_lock:
                    r = gimbal._send_raw("Q\n")
                fx.write(((r or "ERR") + "\n").encode())
            elif cmd == "STAT":
                fx.write(("STAT sent=%d errors=%d pan=%.1f tilt=%.1f\n"
                          % (gimbal.sent, gimbal.errors,
                             gimbal.pan, gimbal.tilt)).encode())
            elif not STRICT:
                # 直通：诊断命令 I/R/V/G/T/N/A/S/L/X 都原样给固件
                # R / V / X 这类要等一会儿才回多行或是不回，超时交给上层
                fwd = cmd[4:] if cmd.startswith("RAW ") else cmd
                with _usb_lock:
                    r = gimbal._send_raw(fwd + "\n")
                fx.write(((r or "ERR no_resp") + "\n").encode())
            else:
                fx.write(b"ERR unknown_cmd\n")
    except (ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:
        print("[bridge] 客户端异常: %s" % e)
    finally:
        try:
            fx.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        print("[bridge] 客户端断开 %s:%d" % addr)


def main():
    global STRICT
    ap = argparse.ArgumentParser(description="云台 USB 桥接服务")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--strict", action="store_true",
                    help="只放行 P/H/Q/STAT（旧行为），其它命令本地回 ERR unknown_cmd")
    args = ap.parse_args()
    STRICT = args.strict

    gimbal = GimbalUSB()
    if gimbal.ser is None:
        print("[-] 无法连接 ESP32，退出", file=sys.stderr)
        return 1

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(4)

    print("=" * 64)
    print("云台桥接已启动")
    print("  监听 %s:%d" % (args.bind, args.port))
    print("  ESP32 通过 USB CDC 连接（不依赖 /dev/ttyACM0）")
    print("=" * 64)

    try:
        while True:
            conn, addr = srv.accept()
            t = threading.Thread(target=handle_client,
                                 args=(conn, addr, gimbal), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\n[退出]")
    finally:
        srv.close()
        gimbal.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
