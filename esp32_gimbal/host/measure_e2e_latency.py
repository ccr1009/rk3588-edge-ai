#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""measure_e2e_latency.py — 精确测量端到端延迟（不靠估算）

测量目标
--------
「人动了 → 舵机开始动」和「人动了 → 舵机到位」这两个延迟。

难点：没有外部触发源（不能让人手拍一下当同步信号）。
解法：**用云台自己的命令链路做探针**。

方法：帧差法
------------
既然舵机运动可以用摄像头像素差测出来（perf-analyst 用过这个方法），
那就发一条已知的角度跳变，用摄像头记录"画面何时开始变、何时稳定"。

    发 P 命令
        ↓ t0（我们发出命令的时刻）
    ESP32 收到并更新 PWM
        ↓ 延迟 = 固件节拍相位 0~20ms
    舵机开始转
        ↓ 机械响应 ~140ms + 6.7ms/deg
    画面开始变化（帧差 > 阈值）
    舵机到位，画面停止变化

测量得到：
    · 命令→画面开始变   = 通信 + 固件节拍 + 舵机启动
    · 命令→画面停止变   = 上面 + 机械运动时间

用法
----
    python3 measure_e2e_latency.py
    python3 measure_e2e_latency.py --steps 10 20 30    # 测多个步长
"""
import argparse
import socket
import sys
import time

import cv2
import numpy as np

BRIDGE = ("127.0.0.1", 9100)
CAM = 83
W, H = 1280, 720


def connect_gimbal():
    s = socket.socket()
    s.settimeout(2.0)
    s.connect(BRIDGE)
    time.sleep(0.3)
    return s


def send_cmd(s, cmd):
    """发命令，不等待回包（测延迟时要尽快返回）。"""
    s.sendall((cmd + "\n").encode())


def open_cam():
    cap = cv2.VideoCapture(CAM, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, H)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)     # 减小缓冲，降低采集延迟
    if not cap.isOpened():
        raise RuntimeError("打不开相机")
    for _ in range(15):
        cap.read()
    return cap


def frame_diff(a, b, region=None):
    """两帧的差异。默认取**局部最大变化**而不是全局平均。

    为什么不用全局平均（2026-11-17 踩坑）：
        画面里有风扇、窗帘、显示器刷新等持续运动的物体，
        全局平均差会被这些东西抬高基线噪声 → 阈值失效 →
        要么测不到（+10° 三次全失败），要么测到几百 ms 的离群值。

    改用：把画面切成 8×8 网格，取**变化最大的那个格子的平均值**。
        舵机转动时，整个场景会平移，很多格子同时变化；
        而风扇只是局部在动 → 单格最大值的信噪比远好于全局平均。
    """
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.int16)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.int16)
    diff = np.abs(ga - gb)

    rows, cols = diff.shape
    gh, gw = rows // 8, cols // 8
    best = 0.0
    for r in range(8):
        for c in range(8):
            blk = diff[r * gh:(r + 1) * gh, c * gw:(c + 1) * gw]
            v = float(blk.mean())
            if v > best:
                best = v
    return best


def measure_once(cap, s, base_pan, base_tilt, step, timeout=2.0):
    """发一次角度跳变，测画面何时开始变、何时停。返回 (start_ms, end_ms, peak)。"""
    # 先回基准位置并稳定
    send_cmd(s, "P %.1f %.1f" % (base_pan, base_tilt))
    time.sleep(1.2)
    for _ in range(5):
        cap.read()

    base = None
    for _ in range(3):
        ok, f = cap.read()
        if ok:
            base = f
    if base is None:
        return None

    # 记录静止时的帧差基线（噪声）
    noise = [frame_diff(base, f) for f in [base] * 1]
    # 实际噪声：连续抓两帧比
    ok, f1 = cap.read()
    ok, f2 = cap.read()
    if ok:
        noise_level = frame_diff(f1, f2)
    else:
        noise_level = 0.5

    # 发跳变命令
    t0 = time.perf_counter()
    send_cmd(s, "P %.1f %.1f" % (base_pan + step, base_tilt))

    threshold = max(noise_level * 2.5, 1.0)   # 高于噪声 2.5 倍算"开始动"
    start_ms = None
    end_ms = None
    peak = 0.0
    last_change = None

    deadline = t0 + timeout
    prev = None
    while time.perf_counter() < deadline:
        ok, f = cap.read()
        if not ok or f is None:
            continue
        now = time.perf_counter()
        d = frame_diff(base, f)
        peak = max(peak, d)

        if start_ms is None and d > threshold:
            start_ms = (now - t0) * 1000

        # "停止变化"：连续两帧的差回到噪声水平
        if prev is not None:
            dd = frame_diff(prev, f)
            if start_ms is not None and dd < threshold * 0.6:
                if last_change is None:
                    last_change = now
                elif (now - last_change) * 1000 > 80:   # 稳定 80ms
                    end_ms = (last_change - t0) * 1000
                    break
            else:
                last_change = None
        prev = f

    if start_ms is None:
        return None
    if end_ms is None:
        end_ms = (time.perf_counter() - t0) * 1000

    return start_ms, end_ms, peak


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", nargs="*", type=float, default=[10, 20, 30],
                    help="测试的 pan 步长（度）")
    ap.add_argument("--repeat", type=int, default=3, help="每个步长重复次数")
    ap.add_argument("--base-pan", type=float, default=0)
    ap.add_argument("--base-tilt", type=float, default=-20)
    args = ap.parse_args()

    print("=" * 78)
    print(" 端到端延迟精确测量（帧差法）")
    print("=" * 78)
    print(" 相机 %dx%d @ /dev/video%d" % (W, H, CAM))

    try:
        cap = open_cam()
    except Exception as e:
        print("❌ %s" % e)
        return 1
    s = connect_gimbal()
    print(" ✅ 云台已连接")

    print()
    print(" pan 步长   序号  开始动(ms)  到位(ms)  峰值差")
    print("-" * 78)

    results = {}
    for step in args.steps:
        starts, ends = [], []
        for i in range(args.repeat):
            r = measure_once(cap, s, args.base_pan, args.base_tilt, step)
            if r is None:
                print("  %+5.0f     %d    ❌ 未检测到运动" % (step, i + 1))
                continue
            st, en, pk = r
            starts.append(st)
            ends.append(en)
            print("  %+5.0f     %d    %7.1f    %7.1f   %5.1f"
                  % (step, i + 1, st, en, pk))
            time.sleep(0.5)
        if starts:
            starts.sort()
            ends.sort()
            results[step] = (starts[len(starts) // 2], ends[len(ends) // 2])

    # 回中
    send_cmd(s, "H")
    time.sleep(0.8)
    s.close()
    cap.release()

    print()
    print("=" * 78)
    print(" 汇总（中位数）")
    print("=" * 78)
    print(" 步长     命令→开始动    命令→到位    机械运动时长")
    print("-" * 78)
    for step in sorted(results):
        st, en = results[step]
        print("  %+5.0f°   %8.1f ms   %8.1f ms   %8.1f ms"
              % (step, st, en, en - st))

    print()
    print(" 说明：")
    print("   · 「命令→开始动」= 通信延迟 + 固件节拍相位 + 舵机启动")
    print("     其中固件节拍是 0~20ms 的量化（50Hz），烧 333Hz 后应为 0~3ms")
    print("   · 「机械运动时长」与舵机速度相关，DS3120 实测 46~85°/s")
    print("   · 这批数据比之前的估算更可信——是帧差实测而非推算")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
