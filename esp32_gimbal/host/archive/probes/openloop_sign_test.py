#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开环阶跃实验：直接测出 pan / tilt 的符号，不靠猜。

为什么要这个脚本：
    闭环跑起来 dx 来回跳时，你无法判断"是方向反了"还是"增益太大"还是"延迟"。
    这三个原因的表现都是"抽风"，光看闭环数据分不开。

    正确做法是**开环阶跃响应**（和伺服系统辨识一样）：
      固定其他轴 → 只给某一个轴一个确定的角度阶跃 → 等稳定 → 读目标位置变化
    这样输入输出一一对应，方向立刻确定。

流程：
    1. 回中，读基准位置 (cx0, cy0)
    2. pan 从 0 → +STEP，读目标新位置 (cx1, cy1)
       云台右转 → 镜头转过去 → 同一目标应该出现在画面**左侧** → cx1 < cx0
       （这就是闭环能收敛的负反馈方向）
       如果 cx1 > cx0 → pan 方向反了，要加 --invert-pan
    3. pan 回 0；tilt 从 0 → +TILT_STEP（抬头），读 cy
       云台抬头 → 目标应该出现在画面**下方** → cy 变大
       如果 cy 变小 → tilt 方向反了，要加 --invert-tilt

用法：
    python openloop_sign_test.py --port COM6 --host <board-ip>
    python openloop_sign_test.py --port COM6 --host <board-ip> --step 20
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


def read_target(sock, want_class, min_score, n=12, timeout=6.0):
    """连续读 n 帧，返回目标中心的中位数（抗单帧误检）。

    为什么用中位数：检测框会有 1~3 像素抖动，偶尔还会丢帧或误检到别的东西。
    取中位数比取最后一帧稳，也比取平均更抗离群值。
    """
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
    return xs[len(xs) // 2], ys[len(ys) // 2], len(xs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--tcp-port", type=int, default=9000)
    ap.add_argument("--class", dest="want_class", default="person")
    ap.add_argument("--min-score", type=float, default=0.40)
    ap.add_argument("--step", type=float, default=20.0, help="pan 阶跃角度（度）")
    ap.add_argument("--tilt-step", type=float, default=15.0, help="tilt 阶跃角度")
    ap.add_argument("--settle", type=float, default=1.5, help="每步等待稳定时间（秒）")
    args = ap.parse_args()

    ser = serial.Serial(args.port, 115200, timeout=0.3, write_timeout=2.0)
    ser.dtr = False
    ser.rts = False
    time.sleep(0.3)
    ser.reset_input_buffer()

    def send(cmd):
        ser.write((cmd + "\n").encode())
        ser.flush()
        time.sleep(0.15)

    sock = socket.create_connection((args.host, args.tcp_port), timeout=5)

    print("=" * 66)
    print("开环阶跃实验  判符号  步长 pan=±%.0f° tilt=±%.0f°" % (args.step, args.tilt_step))
    print("=" * 66)

    def measure(label):
        time.sleep(args.settle)
        r = read_target(sock, args.want_class, args.min_score)
        if r is None:
            print("  %-16s 读不到目标" % label)
            return None
        cx, cy, n = r
        print("  %-16s cx=%+.3f  cy=%+.3f  (%d 帧中位数)" % (label, cx, cy, n))
        return cx, cy

    results = {}

    print("\n[基准] 回中，测目标位置")
    send("H")
    results["zero"] = measure("pan=0 tilt=0")

    print("\n[Pan 阶跃] pan 0 → +%.0f" % args.step)
    send("P %.1f 0" % args.step)
    results["pan_pos"] = measure("pan=+%.0f" % args.step)

    print("\n[Pan 阶跃] pan 0 → -%.0f" % args.step)
    send("P -%.1f 0" % -args.step)
    results["pan_neg"] = measure("pan=-%.0f" % args.step)

    print("\n[Tilt 阶跃] tilt 0 → +%.0f（抬头）" % args.tilt_step)
    send("P 0 %.1f" % args.tilt_step)
    results["tilt_pos"] = measure("tilt=+%.0f" % args.tilt_step)

    print("\n[Tilt 阶跃] tilt 0 → -%.0f（低头）" % -args.tilt_step)
    send("P 0 %.1f" % -args.tilt_step)
    results["tilt_neg"] = measure("tilt=-%.0f" % args.tilt_step)

    send("H")
    ser.close()
    sock.close()

    # ---------------- 判读 ----------------
    print("\n" + "=" * 66)
    print("判读")
    print("=" * 66)

    z = results["zero"]
    if z is None:
        print("基准读不到目标，无法判读。让目标站在画面中央重试。")
        return 1

    ok_pan = None
    ok_tilt = None

    pp = results["pan_pos"]
    if pp:
        # 云台右转(pan 增大) → 镜头转过去 → 目标落到画面左侧 → cx 减小
        d = pp[0] - z[0]
        print("\nPan: 云台 0 → +%.0f°，目标 cx 变化 %+.3f" % (args.step, d))
        if d < -0.02:
            print("  ✅ 方向正确（云台右转 → 目标左移 → 负反馈）")
            ok_pan = False
        elif d > 0.02:
            print("  ❌ 方向反了（云台右转 → 目标右移 → 正反馈，会顶死）")
            print("     → 加 --invert-pan")
            ok_pan = True
        else:
            print("  ⚠️  变化太小（%.3f），加大 --step 重试" % abs(d))

    tp = results["tilt_pos"]
    if tp:
        d = tp[1] - z[1]
        print("\nTilt: 云台 0 → +%.0f°（抬头），目标 cy 变化 %+.3f" % (args.tilt_step, d))
        if d > 0.02:
            print("  ✅ 方向正确（抬头 → 目标在画面里往下走 → 负反馈）")
            ok_tilt = False
        elif d < -0.02:
            print("  ❌ 方向反了 → 加 --invert-tilt")
            ok_tilt = True
        else:
            print("  ⚠️  变化太小（%.3f），加大 --tilt-step 重试" % abs(d))

    print("\n" + "-" * 66)
    if ok_pan is None and ok_tilt is None:
        print("都没测出有效变化。先确认：目标在画面里可见、且位置有明显偏差。")
        return 1
    flags = []
    if ok_pan:
        flags.append("--invert-pan")
    if ok_tilt:
        flags.append("--invert-tilt")
    if flags:
        print("建议加参数: %s" % " ".join(flags))
    else:
        print("两个轴方向都正确，不需要 invert。若仍振荡，是 kp 太大，改 --kp-pan/--kp-tilt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
