#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_p2_after_flash.py — 烧完 333Hz 固件后的验证

背景
----
P2（PWM 50Hz→333Hz + 控制循环 20ms→3ms）的固件已编译好，
但**板子没有 /dev/ttyACM*（内核未编 cdc_acm 模块），无法在板上烧录**。
必须在 PC 上烧（ESP32 插 PC 的 USB 口，用 build.ps1 -Flash）。

烧完后跑这个脚本验证。判断依据是**串口往返延迟**：

    50Hz 固件（旧）  → ESP32 控制循环 20ms → 延迟严格等于 (20ms - 相位)，均值 10ms
    333Hz 固件（新） → ESP32 控制循环  3ms → 延迟严格等于 ( 3ms - 相位)，均值 1.5ms

所以只要测 p50，就能确定烧没烧上，不需要接示波器。

用法
----
    python3 check_p2_after_flash.py
    python3 check_p2_after_flash.py --expect 333    # 明确断言应该是 333Hz
"""
import argparse
import socket
import sys
import time


def connect(timeout=3.0):
    s = socket.socket()
    s.settimeout(timeout)
    s.connect(("127.0.0.1", 9100))
    time.sleep(0.3)
    return s


def once(s, cmd, timeout=2.0):
    """发一条命令，返回 (应答行, 耗时ms)。

    ★ 必须「首次收到非注释应答就返回」，不能用固定 wait 循环
      （2026-11-16 踩过：那样测到的是等待时间不是延迟）。
    """
    t0 = time.perf_counter()
    s.sendall((cmd + "\n").encode())
    s.settimeout(timeout)
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


def measure(s, n=30):
    for _ in range(3):          # 预热
        once(s, "Q")
    lats = []
    for _ in range(n):
        _, ms = once(s, "Q")
        lats.append(ms)
        time.sleep(0.02)
    lats.sort()
    return {
        "p50": lats[len(lats) // 2],
        "p90": lats[int(len(lats) * 0.9)],
        "max": lats[-1],
        "min": lats[0],
    }


def detect_freq(stats):
    """从延迟推断固件节拍频率。"""
    p50 = stats["p50"]
    # 理论：p50 ≈ 周期/2
    period_ms = p50 * 2.0
    freq = 1000.0 / period_ms if period_ms > 0 else 0
    if p50 < 4.0:
        return 333, "333Hz"
    elif p50 < 12.0:
        return 100, "约 100Hz"
    elif p50 < 30.0:
        return 50, "50Hz（旧固件）"
    else:
        return None, "延迟异常"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", type=int, choices=[50, 333], default=None,
                    help="期望的固件频率，用于明确断言")
    args = ap.parse_args()

    print("=" * 74)
    print(" P2 验证：ESP32 固件是 50Hz 还是 333Hz")
    print("=" * 74)

    try:
        s = connect()
    except Exception as e:
        print("❌ 连不上桥接 9100: %s" % e)
        print("   先启动：sudo systemctl start gimbal-bridge")
        return 1

    print(" 引脚: %s" % once(s, "I", 1.0)[0])

    print()
    print("▶ 测量串口往返延迟（Q 命令 × 30）")
    print("-" * 74)
    st = measure(s)
    print("   min=%.2f  p50=%.2f  p90=%.2f  max=%.2f ms"
          % (st["min"], st["p50"], st["p90"], st["max"]))

    freq, label = detect_freq(st)
    print()
    if freq == 333:
        print(" ✅ 判定：%s —— 控制循环 3ms，P2 烧录成功！" % label)
        print("    端到端延迟从 262ms 降到约 253ms")
    elif freq == 50:
        print(" ⏸️ 判定：%s —— 控制循环 20ms，**P2 还没烧**" % label)
        print()
        print("    烧录方法（ESP32 插 PC 的 USB 口）：")
        print("      cd D:\\a研究生\\项目\\rk3588项目\\edgevision-rk3588\\esp32_gimbal")
        print("      pwsh -File build.ps1 -Flash")
        print()
        print("    烧完应该看到 p50 从 ~20ms 降到 ~1.5ms")
    else:
        print(" ⚠️ 判定：%s（p50=%.2fms）" % (label, st["p50"]))
        print("    可能桥接或 USB 通路有异常，先查 errors")

    # 断言
    if args.expect is not None and freq is not None:
        print()
        if freq == args.expect:
            print(" ✅ 断言通过：实测 %dHz == 期望 %dHz" % (freq, args.expect))
            rc = 0
        else:
            print(" ❌ 断言失败：实测 %dHz != 期望 %dHz" % (freq, args.expect))
            rc = 1
    else:
        rc = 0

    # 顺带确认服务整体健康
    print()
    print("▶ 顺带确认服务健康")
    print("-" * 74)
    try:
        import json
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8080/status", timeout=3) as r:
            j = json.loads(r.read())
        print("   fps=%.2f  npu=%.2fms  errors=%d  dropped=%d"
              % (j["fps"], j["npu_ms"], j["errors"], j["dropped"]))
        if j["fps"] >= 29.0 and j["errors"] == 0:
            print("   ✅ 帧率正常、零错误")
        else:
            print("   ⚠️ 需要关注")
    except Exception as e:
        print("   ⚠️ 读 /status 失败: %s" % e)

    s.close()
    print()
    print("=" * 74)
    return rc


if __name__ == "__main__":
    sys.exit(main())
