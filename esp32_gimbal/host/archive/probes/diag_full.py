#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_full.py — 一屏看清「固件对不对 + PWM 出没出 + 舵机动没动」

背景（2026-11-05）：
    之前排查舵机时发 I / R 14 / V 14 / G 14 / T 15 全部收到 "ERR unknown_cmd"，
    误判成「固件没烧进去」。真正原因是 gimbal_bridge.py 只放行 P/H/Q/STAT 四条，
    其余本地回了 ERR unknown_cmd（**不带数字**）。固件自己回的是 "ERR 9 unknown_cmd"。

    所以这份脚本第一件事就是把两个来源分开：
        "ERR 9 xxx"        → 固件回的（命令确实不认识）
        "ERR unknown_cmd"  → 桥回的（命令根本没到 ESP32）
        "ERR no_resp"      → 桥转发了，但 ESP32 超时没回

跑法（板子上）：
    /root/rknn_env/bin/python /root/esp32_gimbal/diag_full.py

判读：
    [A] I 回 "GPIN 14 15"            → 固件是 v2，一切正常
    [B] R 14 回 pw_avg≈1500us        → GPIO14 真有 50Hz 波形，问题在舵机侧
        R 14 回 edges=0 NO_SIGNAL    → 引脚没波形，问题在固件/引脚
    [C] 摆动时舵机不动但要靠眼睛看
"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 9100


def connect():
    try:
        s = socket.create_connection((HOST, PORT), timeout=3)
    except OSError as e:
        print("连不上桥接 %s:%d -> %s" % (HOST, PORT, e))
        print("先启动： /root/rknn_env/bin/python /root/esp32_gimbal/gimbal_bridge.py --port 9100")
        sys.exit(1)
    s.settimeout(0.5)
    return s, s.makefile("rwb", buffering=0)


def drain(f, dur=1.0):
    out = []
    t0 = time.time()
    while time.time() - t0 < dur:
        try:
            line = f.readline()
            if line:
                v = line.decode("utf-8", "replace").strip()
                if v:
                    out.append(v)
        except Exception:
            time.sleep(0.05)
    return out


def send(f, cmd, wait=0.8, dur=0.0):
    """发一条，收 wait 秒内的第一行；dur>0 时改成收满 dur 秒的所有行"""
    f.write((cmd + "\n").encode())
    time.sleep(wait)
    if dur > 0:
        return drain(f, dur)
    try:
        return f.readline().decode("utf-8", "replace").strip()
    except Exception:
        return "(超时)"


def classify(resp):
    """把回包归因，这是整份脚本的核心"""
    if not isinstance(resp, str):
        return "?"
    if resp.startswith("ERR no_resp"):
        return "网桥转了但板子没回"
    if resp.startswith("ERR ") and resp[4:5].isdigit():
        return "固件回的（命令不存在）"
    if resp.startswith("ERR unknown_cmd"):
        return "★桥挡了（命令没到 ESP32）"
    if resp.startswith("ERR"):
        return "其它错误"
    return "OK"


def main():
    s, f = connect()

    print("=" * 70)
    print("云台全链路诊断")
    print("=" * 70)

    # ---------------- A. 固件身份 ----------------
    print()
    print("[A] 固件身份 —— 如果桥是旧版，这里会全是「桥挡了」")
    print("    " + "-" * 62)
    for cmd in ["I", "Q", "P 0 0", "S 100"]:
        r = send(f, cmd, 0.7)
        print("    %-10s -> %-28r  %s" % (cmd, r, classify(r)))

    r_i = send(f, "I", 0.7)
    is_v2 = isinstance(r_i, str) and r_i.startswith("GPIN")
    print()
    if is_v2:
        print("    ✅ 拿到 GPIN %s → 固件是 v2，之前「unknown_cmd」确实是桥的锅"
              % r_i.replace("GPIN", "").strip())
    else:
        print("    ❌ I 没回 GPIN（%r）→ 要么桥还是旧版，要么固件真不是 v2" % r_i)
        print("       先确认桥是新的：bridge 里应有 `elif not STRICT:` 直通分支")

    # ---------------- B. 引脚波形自测 ----------------
    print()
    print("[B] GPIO14 回环自测 —— 这一步把「固件问题」和「舵机问题」彻底分开")
    print("    " + "-" * 62)
    send(f, "P 0 0", 0.9)
    drain(f, 0.3)

    send(f, "R 14", 0.5)
    ack = drain(f, 0.6)
    print("    R 14 ACK : %r" % (ack[:6] if ack else "(空)"))
    if ack and any("ERR" in x and "unknown" in x for x in ack):
        print("    ❌ 固件不认识 R 命令 —— 这份固件不是 v2，别再查舵机，先烧固件")

    print("    收集 4 秒统计 ...")
    lines = drain(f, 4.0)
    lb = [x for x in lines if "LB" in x or "edges" in x]
    pw_ok = False
    if lb:
        for x in lb[-6:]:
            print("      %s" % x)
        joined = " ".join(lb)
        if "edges=0" in joined or "NO_SIGNAL" in joined:
            print()
            print("    ❌ GPIO14 上 0 个边沿 → 引脚没有波形")
            print("       → 问题在固件/引脚配置，跟舵机、供电、杜邦线都无关")
        elif "pw_avg" in joined:
            pw_ok = True
            print()
            print("    ✅ 有波形。看 pw_avg 是否 ≈1500us，per_avg 是否 ≈20000us")
            print("       → 信号确实出到 pad 了，舵机不动只能查：供电 / 共地 / 信号线 / 舵机本身")
    else:
        print("    ⚠️  没收到 LB 行。收到的原始输出：")
        for x in lines[-8:]:
            print("       %s" % x)
        print("    （R 命令是否被支持，看上面 ACK）")

    send(f, "R -1", 0.4)
    drain(f, 0.4)

    # ---------------- C. 满量程再测一次 ----------------
    if pw_ok:
        print()
        print("[C] P 45 0 —— 脉宽应该从 1500us 掉到约 2000us（或反过来）")
        print("    " + "-" * 62)
        send(f, "P 45 0", 1.0)
        lines2 = drain(f, 3.5)
        lb2 = [x for x in lines2 if "LB" in x or "edges" in x]
        for x in lb2[-4:]:
            print("      %s" % x)
        if not lb2:
            print("      (没收到 LB 行)")
        send(f, "H", 0.8)
        drain(f, 0.4)

    # ---------------- D. 多路电源 / 舵机裸测命令 ----------------
    print()
    print("[D] 下一步的两种走法（都在这里给出，你挑一个）")
    print("    " + "-" * 62)
    print("    如果 [B] 有波形 pw≈1500us → 固件无罪，去查硬件：")
    print("        · 舵机红线 有没有真的插在 5V 上（用万用表量 OUT+ 对 OUT- 应 5V）")
    print("        · 舵机黑/棕线 和 ESP32 GND 有没有共地")
    print("        · 换一个舵机试（排除舵机本身坏）")
    print()
    print("    如果 [B] 是 edges=0 → 固件/引脚问题：")
    print("        · 运行  N 14   进入裸 GPIO 翻转（300ms 方波，万用表好量）")
    print("          停止  A")
    print("          若 300ms 方波量得到 0↔3.3V 跳变，说明引脚本身好的，是 LEDC 没接上")

    print()
    print("=" * 70)
    s.close()


if __name__ == "__main__":
    main()
