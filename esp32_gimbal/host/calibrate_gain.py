#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标定「云台角度 → 画面位移」的传递增益。

为什么必须标定：
    增量式闭环的收敛速度完全取决于这个增益。它由几何决定，不是常数：
      - 摄像头装在云台**转轴上** → 纯旋转 → 增益接近 1.0（转 1° 画面移 1°）
      - 摄像头装在平台**边缘** → 转动时主要是平移 → 增益远小于 1
      - 摄像头与云台转向不同步 → 增益会小于 0.3

    实测在 0.22 左右时，要消除 0.33 的归一化偏差需要云台转 90°，必然顶死。
    这种"能追但追不到"的现象，光看闭环数据分不清是"参数不对"还是"机械布局不行"。
    标定之后一眼就能看出：是 kp 该调，还是摄像头得挪位置。

输出:
    gain_pan  = (画面位移角度) / (云台转动角度)
    gain_tilt = 同上
    gain ≈ 1.0  → 理想，抖动少，kp 可以大
    gain < 0.4  → 摄像头偏离转轴太远，建议挪到转轴附近
    gain < 0    → 方向反了，加 --invert-pan

用法:
    python calibrate_gain.py --port COM6 --host <board-ip>
    python calibrate_gain.py --port COM6 --host <board-ip> --range 40 --step 20
"""
import argparse
import os
import socket
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

try:
    import serial
except ImportError:
    print("缺 pyserial", file=sys.stderr)
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gimbal_control import FRAME_SIZE, parse_frame, pick_target


def recv_frame(sock, timeout=0.3):
    buf = bytearray()
    sock.settimeout(timeout)
    while len(buf) < FRAME_SIZE:
        try:
            chunk = sock.recv(FRAME_SIZE - len(buf))
        except (socket.timeout, ConnectionResetError, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def measure(sock, want_class, min_score, n=8, timeout=4.0):
    xs, ys = [], []
    t0 = time.time()
    while len(xs) < n and time.time() - t0 < timeout:
        raw = recv_frame(sock)
        if raw is None:
            continue
        try:
            _, boxes = parse_frame(raw, 1280, 720)
        except Exception:
            continue
        tgt = pick_target(boxes, want_class, min_score)
        if tgt is None:
            continue
        xs.append(tgt["cx"])
        ys.append(tgt["cy"])
    if not xs:
        return None
    xs.sort()
    ys.sort()
    return xs[len(xs) // 2], ys[len(ys) // 2]


def linfit(xs, ys):
    """最小二乘拟合 y = a*x + b，返回 (a, b)。"""
    n = len(xs)
    if n < 2:
        return None, None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None, None
    return num / den, my - num / den * mx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--tcp-port", type=int, default=9000)
    ap.add_argument("--class", dest="want_class", default="person")
    ap.add_argument("--min-score", type=float, default=0.40)
    ap.add_argument("--range", type=float, default=40.0, help="pan 扫描范围 ±N 度")
    ap.add_argument("--step", type=float, default=20.0)
    ap.add_argument("--tilt-range", type=float, default=30.0)
    ap.add_argument("--tilt-step", type=float, default=15.0)
    ap.add_argument("--settle", type=float, default=0.7)
    ap.add_argument("--fov-pan", type=float, default=60.0)
    ap.add_argument("--fov-tilt", type=float, default=36.0)
    args = ap.parse_args()

    ser = serial.Serial(args.port, 115200, timeout=0.3, write_timeout=2.0)
    ser.dtr = False
    ser.rts = False
    time.sleep(0.3)
    ser.reset_input_buffer()

    def send(cmd):
        ser.write((cmd + "\n").encode())
        ser.flush()
        time.sleep(0.1)

    sock = socket.create_connection((args.host, args.tcp_port), timeout=5)

    print("=" * 68)
    print("云台→画面 传递增益标定")
    print("  pan  ±%.0f° 每步 %.0f°   tilt ±%.0f° 每步 %.0f°"
          % (args.range, args.step, args.tilt_range, args.tilt_step))
    print("=" * 68)

    def sweep(axis, lo, hi, step):
        """扫描一个轴，返回 [(angle, cx, cy)]"""
        pts = []
        ang = lo
        while ang <= hi + 1e-6:
            if axis == "pan":
                send("P %.1f 0" % ang)
            else:
                send("P 0 %.1f" % ang)
            time.sleep(args.settle)
            m = measure(sock, args.want_class, args.min_score)
            if m is None:
                print("  %s=%+6.1f  → 读不到目标" % (axis, ang))
            else:
                print("  %s=%+6.1f  → cx=%+.3f  cy=%+.3f" % (axis, ang, m[0], m[1]))
                pts.append((ang, m[0], m[1]))
            ang += step
        return pts

    print("\n[Pan 扫描]")
    pan_pts = sweep("pan", -args.range, args.range, args.step)
    send("H")
    time.sleep(0.5)

    print("\n[Tilt 扫描]")
    tilt_pts = sweep("tilt", -args.tilt_range, args.tilt_range, args.tilt_step)
    send("H")

    ser.close()
    sock.close()

    print("\n" + "=" * 68)
    print("结果")
    print("=" * 68)

    if len(pan_pts) >= 3:
        angs = [p[0] for p in pan_pts]
        cxs = [p[1] for p in pan_pts]
        slope, _ = linfit(angs, cxs)     # d(cx)/d(pan)
        if slope is not None:
            # cx 是归一化的（0~1），乘 fov 换成角度
            gain_pan = -slope * args.fov_pan
            print("\nPan: d(cx)/d(pan) = %+.5f /度" % slope)
            print("     换算增益 gain_pan = %+.2f" % gain_pan)
            print("     （正=负反馈，可收敛；负=方向反）")
            print("     注：原始数据点数 %d" % len(pan_pts))

    if len(tilt_pts) >= 3:
        angs = [p[0] for p in tilt_pts]
        cys = [p[2] for p in tilt_pts]
        slope, _ = linfit(angs, cys)
        if slope is not None:
            gain_tilt = slope * args.fov_tilt
            print("\nTilt: d(cy)/d(tilt) = %+.5f /度" % slope)
            print("      换算增益 gain_tilt = %+.2f" % gain_tilt)

    print("\n" + "-" * 68)
    print("怎么用这个数：")
    print("  gain ≈ 1.0  理想（摄像头在转轴上），kp 可以到 0.4~0.5")
    print("  gain ≈ 0.5  kp 上限约 0.25")
    print("  gain < 0.4  摄像头离转轴太远，转动变平移，建议挪位置")
    print("  gain < 0    方向反了，加 --invert-pan / --invert-tilt")
    print("\n经验公式：kp 上限 ≈ 0.5 / gain   （超过就振荡）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
