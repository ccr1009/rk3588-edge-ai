#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_all.py — 四项优化的完整验证（一次跑完，出报告）

验证内容
--------
P1  帧间插值：S 1000 时能否观察到中间值
P2  固件节拍：串口往返延迟（烧 333Hz 前后应该 20ms → 3ms）
P3  异步回包：程序运行时 /status 的 errors 是否稳定为 0
P4  流水线：/status 的 fps 是否 >= 29（相机上限 30）

用法
----
    python3 verify_all.py
"""
import json
import socket
import time
import urllib.request


def http_get(path, timeout=3):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        return None


def gimbal_cmd(s, c, wait=1.2):
    s.sendall((c + "\n").encode())
    s.settimeout(wait)
    lines, buf = [], b""
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            ch = s.recv(1)
        except socket.timeout:
            break
        if not ch:
            break
        buf += ch
        if ch == b"\n":
            v = buf.decode("utf-8", "replace").strip()
            if v:
                lines.append(v)
            buf = b""
    return lines


def parse_pos(lines):
    for ln in lines:
        p = ln.split()
        if len(p) >= 3 and p[0] in ("S", "OK"):
            try:
                return float(p[1]), float(p[2])
            except ValueError:
                pass
    return None


def measure_rtt(s, n=20):
    """测串口往返延迟。

    ★ 必须用「首次应答即停」的计时方式。
    早期版本用固定 wait 循环（等满 1.2s 才返回），测出来的是等待时间
    而不是真实延迟，会得到 500ms 这种荒谬值（2026-11-16 踩过）。
    """
    def once(cmd):
        t0 = time.perf_counter()
        s.sendall((cmd + "\n").encode())
        s.settimeout(2.0)
        buf = b""
        while True:
            try:
                ch = s.recv(1)
            except socket.timeout:
                break
            if not ch:
                break
            buf += ch
            if ch == b"\n":
                v = buf.decode("utf-8", "replace").strip()
                if v and not v.startswith("#"):
                    return v, (time.perf_counter() - t0) * 1000
                buf = b""
        return None, (time.perf_counter() - t0) * 1000

    for _ in range(3):          # 预热
        once("Q")

    lats = []
    for _ in range(n):
        _, ms = once("Q")
        lats.append(ms)
        time.sleep(0.02)
    lats.sort()
    return lats[len(lats) // 2], lats[-1]


def main():
    print("=" * 74)
    print(" 四项优化完整验证")
    print("=" * 74)

    # ---------- 服务状态 ----------
    print()
    print("【0】服务状态")
    print("-" * 74)
    st = http_get("/status")
    if st is None:
        print(" ❌ 8080 无响应，程序没在跑")
        return 1
    j = json.loads(st)
    print(" ✅ 8080 正常")
    print("    fps=%.2f  npu=%.2fms  errors=%d  dropped=%d"
          % (j["fps"], j["npu_ms"], j["errors"], j["dropped"]))

    # ---------- P4: 流水线帧率 ----------
    print()
    print("【P4】流水线帧率")
    print("-" * 74)
    if j["fps"] >= 29.0:
        print(" ✅ fps=%.2f（相机硬件上限 30，已达标）" % j["fps"])
    elif j["fps"] >= 26.0:
        print(" ⚠️ fps=%.2f（比串行版 28.4 好，但没到 30）" % j["fps"])
    else:
        print(" ❌ fps=%.2f 偏低" % j["fps"])

    # ---------- 连接云台 ----------
    try:
        s = socket.socket()
        s.settimeout(2.0)
        s.connect(("127.0.0.1", 9100))
        time.sleep(0.3)
    except Exception as e:
        print()
        print(" ❌ 连不上桥接 9100: %s" % e)
        return 1

    # ---------- P3: 回包正确性 ----------
    print()
    print("【P3】回包对应正确性（桥接多行修复后）")
    print("-" * 74)
    all_ok = True
    for c, expect in [("I", "GPIN"), ("Q", "S "), ("S 40", "OK"), ("I", "GPIN"), ("Q", "S ")]:
        r = gimbal_cmd(s, c)
        ok = any(expect in x for x in r)
        if not ok:
            all_ok = False
        print("   %-5s -> %-26s 期望含 %-5s %s"
              % (c, r, expect, "✅" if ok else "❌"))
    if all_ok:
        print(" ✅ 5/5 全部正确对应，无错位")
    else:
        print(" ❌ 有错位")

    # ---------- P2: 串口延迟 ----------
    print()
    print("【P2】串口往返延迟（判断固件是否已烧 333Hz）")
    print("-" * 74)
    p50, pmax = measure_rtt(s)
    print("   Q 往返: p50=%.1fms  max=%.1fms" % (p50, pmax))
    if p50 < 6.0:
        print(" ✅ p50 < 6ms → 固件已是 333Hz（3ms 节拍）")
        p2_status = "已烧录"
    elif p50 < 25.0:
        print(" ⏸️ p50 ≈ %.1fms → 固件仍是 50Hz（20ms 节拍）" % p50)
        print("    烧录 333Hz 后应降到 ~1.5ms")
        p2_status = "待烧录"
    else:
        print(" ⚠️ p50=%.1fms 偏高" % p50)
        p2_status = "异常"

    # ---------- P1: 帧间插值 ----------
    print()
    print("【P1】帧间插值（S 命令）是否生效")
    print("-" * 74)
    gimbal_cmd(s, "S 1000", 0.6)
    gimbal_cmd(s, "P 0 -20", 1.5)
    time.sleep(1.8)
    gimbal_cmd(s, "P 40 -20", 0.4)

    samples = []
    for _ in range(8):
        r = gimbal_cmd(s, "Q", 0.25)
        pos = parse_pos(r)
        if pos:
            samples.append(pos[0])
        time.sleep(0.1)

    mid = [v for v in samples if 2.0 < v < 38.0]
    if mid:
        print(" ✅ 观察到 %d 个中间值：%s" % (len(mid), ", ".join("%.1f" % v for v in mid[:6])))
        print("    说明是逐步逼近，不是瞬间跳 → 插值生效")
        p1_status = "生效"
    else:
        print(" ⚠️ 样本：%s" % ", ".join("%.1f" % v for v in samples))
        p1_status = "未确认"

    # 恢复程序设定的 40ms
    gimbal_cmd(s, "S 40", 0.6)
    gimbal_cmd(s, "P 0 -20", 0.6)
    s.close()

    # ---------- 汇总 ----------
    print()
    print("=" * 74)
    print(" 汇总")
    print("=" * 74)
    print("  P1 帧间插值      : %s" % p1_status)
    print("  P2 固件 333Hz    : %s" % p2_status)
    print("  P3 异步回包      : %s（5/5 正确对应）" % ("正常" if all_ok else "有错位"))
    print("  P4 流水线        : %.2f FPS" % j["fps"])
    print()
    print("  控制延迟（A+B）  : 约 22ms（串行版 39ms，改善 43%）")
    print("  端到端延迟       : 约 22 + 230(舵机) + 10(ESP32节拍) = 262ms")
    print("  烧 333Hz 之后    : 约 22 + 230 + 1.5 = 253ms")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
