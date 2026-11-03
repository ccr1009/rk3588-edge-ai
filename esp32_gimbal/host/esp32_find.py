"""自动找出 ESP32 在本机的 COM 口。

为什么需要：这块核心板有两个 Type-C：
    COM 口 → 经 CH343 转 UART0，VID_1A86 PID_55D4 / 7523
    USB 口 → 原生 USB-Serial-JTAG，VID_303A PID_1001
插哪个口，Windows 给的 COM 号都不一样（COM5/COM6/COM7 都见过）。
与其每次猜，不如按 VID/PID 认设备。

用法：
    python esp32_find.py            # 只列出
    python esp32_find.py --send "G 14" "D 1"    # 找到就发命令
"""
import argparse
import re
import subprocess
import sys
import time

try:
    import serial
except ImportError:
    print("缺 pyserial: pip install pyserial", file=sys.stderr)
    sys.exit(2)


KNOWN = {
    ("303A", "1001"): "ESP32-S3 原生USB (USB口)",
    ("1A86", "55D4"): "CH343 (COM口)",
    ("1A86", "7523"): "CH340/CH343 (COM口)",
    ("1A86", "55D3"): "CH343 (COM口)",
    ("10C4", "EA60"): "CP210x",
}


def list_ports():
    """返回 [(com, vid, pid, desc)]，只保留能找到 VID/PID 的串口。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match 'COM\\d+' } | "
             "Select-Object Name,DeviceID | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception as e:
        print("枚举串口失败: %s" % e, file=sys.stderr)
        return []

    import json
    try:
        data = json.loads(out) if out.strip() else []
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]

    ports = []
    for item in data:
        name = item.get("Name") or ""
        devid = item.get("DeviceID") or ""
        m = re.search(r"COM(\d+)", name)
        if not m:
            continue
        com = "COM" + m.group(1)
        vm = re.search(r"VID_([0-9A-Fa-f]{4})", devid)
        pm = re.search(r"PID_([0-9A-Fa-f]{4})", devid)
        vid = vm.group(1).upper() if vm else ""
        pid = pm.group(1).upper() if pm else ""
        desc = KNOWN.get((vid, pid), name)
        ports.append((com, vid, pid, desc))
    return ports


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", nargs="*", default=None,
                    help="找到 ESP32 后依次发送这些命令")
    ap.add_argument("--seconds", type=float, default=8.0)
    args = ap.parse_args()

    ports = list_ports()
    if not ports:
        print("没找到任何带 VID/PID 的串口。USB 线插了吗？")
        return 1

    print("检测到的串口：")
    target = None
    for com, vid, pid, desc in ports:
        mark = ""
        if vid == "303A" or vid == "1A86":
            mark = "  <== ESP32 候选"
            if target is None:
                target = com
        print("  %-6s VID_%s PID_%s  %s%s" % (com, vid, pid, desc, mark))

    if target is None:
        print("\n没识别出 ESP32。若只有 CH340/CP210x 之类，请手动指定 --port。")
        return 1

    print("\n使用 %s" % target)

    if args.send is None:
        return 0

    s = serial.Serial()
    s.port = target
    s.baudrate = 115200
    s.timeout = 0.3
    s.write_timeout = 2.0
    s.open()
    s.dtr = False
    s.rts = False
    time.sleep(0.4)
    s.reset_input_buffer()

    for cmd in args.send:
        try:
            s.write((cmd + "\n").encode())
            s.flush()
        except Exception as e:
            print("  发 %-10s 失败: %s" % (cmd, e))
            continue
        time.sleep(0.5)
        n = s.in_waiting
        resp = (s.read(n) if n else b"").decode("utf-8", "replace").strip()
        # 去掉启动日志里的 I (...) 行
        lines = [l for l in resp.splitlines()
                 if l.strip() and not re.match(r"^[IEWD] \(\d+\)", l.strip())]
        print("  %-10s -> %s" % (cmd, " | ".join(lines) if lines else "(无响应)"))

    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
