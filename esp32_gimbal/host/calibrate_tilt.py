#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibrate_tilt.py — tilt 轴方向标定（接上第二颗舵机后跑这个）

为什么单独做 tilt 的标定，不照抄 pan 的结论：
    2026-11-03 实测 pan 和 tilt **两个轴都反**（run_tracking.ps1 里记着：
    只加 invert-pan 时 tilt 仍顶在 -45）。但那是"摄像头倒装 180°"的典型特征，
    也可能是舵机绕线方向不同。换一颗舵机、改一次安装，结论就可能变。
    所以每次动硬件都该重跑，不要相信上一次的结论。

原理（和 pan 一样，物理直觉 + 实测验证）：
    摄像头装在云台上，跟着抬头/低头。
    云台抬头（tilt 增大）→ 镜头朝上 → 原本在画面上方的目标
    相对画面会**往下移**（因为镜头追上去了）。
    所以：tilt 增大 → cy 减小 = 负反馈，方向正确，不需要 invert-tilt
          tilt 增大 → cy 增大 = 正反馈，需要 invert-tilt

用法（板端）：
    /root/rknn_env/bin/python calibrate_tilt.py
    /root/rknn_env/bin/python calibrate_tilt.py --port /dev/ttyUSB0
    /root/rknn_env/bin/python calibrate_tilt.py --angles -30 0 30

前提：
    · 9000 检测流在跑（一体化程序或 edgevision 网关都行）
    · 云台串口没有被其他进程占用（先停掉追踪程序）
"""
import argparse
import statistics
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
import serial
from gimbal_control import FRAME_SIZE, parse_frame, COCO80


def grab_frame(port=9000, timeout=1.0, img_w=1280, img_h=720):
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return None
    buf = bytearray()
    s.settimeout(timeout)
    try:
        while len(buf) < FRAME_SIZE:
            c = s.recv(FRAME_SIZE - len(buf))
            if not c:
                break
            buf.extend(c)
    except Exception:
        pass
    finally:
        s.close()
    if len(buf) != FRAME_SIZE:
        return None
    return parse_frame(bytes(buf), img_w, img_h)


def main():
    ap = argparse.ArgumentParser(description="tilt 轴方向标定")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--angles", nargs=3, type=float, default=[-20, 0, 20],
                    help="三个 tilt 角度")
    ap.add_argument("--frames", type=int, default=15, help="每个角度采多少帧")
    ap.add_argument("--min-score", type=float, default=0.5)
    args = ap.parse_args()

    IMG_W, IMG_H = 1280, 720
    PERSON = COCO80.index("person")

    ser = serial.Serial(args.port, 115200, timeout=1.0)
    time.sleep(0.3)
    ser.reset_input_buffer()

    def send(cmd, wait=1.0):
        ser.write((cmd + "\n").encode())
        ser.flush()
        time.sleep(wait)
        return ser.read(300).decode(errors="replace").strip()

    def sample(n):
        ys = []
        last = None
        t0 = time.time()
        while len(ys) < n and time.time() - t0 < 8:
            r = grab_frame()
            if r is None:
                continue
            fid, boxes = r
            if fid == last:
                time.sleep(0.01)
                continue
            last = fid
            cand = [b for b in boxes
                    if b["class_id"] == PERSON and b["score"] >= args.min_score]
            if not cand:
                continue
            best = max(cand, key=lambda b: b["w"] * b["h"])
            ys.append(best["cy"] * IMG_H)
        return ys

    print("=" * 68)
    print("tilt 轴方向标定")
    print("=" * 68)
    print()

    print("回中 ...", send("H", 1.2))
    print()

    results = {}
    for t in args.angles:
        print("--- tilt = %+d ---" % t)
        print("  下发:", send("P 0 %d" % t, 1.0))
        time.sleep(0.6)          # 等缓动到位
        ys = sample(args.frames)
        results[t] = ys
        if ys:
            print("  cy: n=%d 均值=%.1f 中位=%.1f 范围[%.1f, %.1f]"
                  % (len(ys), statistics.mean(ys), statistics.median(ys),
                     min(ys), max(ys)))
        else:
            print("  (没采到 person —— 画面里要有人，且置信度 >%.1f)" % args.min_score)
        print()

    valid = {t: ys for t, ys in results.items() if len(ys) >= 8}
    print("=" * 68)
    if len(valid) < 2:
        print("样本不足，无法判定。请确保：")
        print("  1. 画面里有人（站 1.5~3m，能框到上半身）")
        print("  2. tilt 舵机已接上且能转（用 P 0 20 肉眼确认）")
    else:
        tmin, tmax = min(valid), max(valid)
        cy_low = statistics.median(valid[tmin])
        cy_high = statistics.median(valid[tmax])
        delta = cy_high - cy_low

        print("Δcy = cy(tilt=%+d) - cy(tilt=%+d) = %.1f - %.1f = %+.1f px"
              % (tmax, tmin, cy_high, cy_low, delta))
        print()
        threshold = 25.0
        if delta < -threshold:
            print("判定：tilt↑ → cy↓（目标在画面里往下移）")
            print("      = 负反馈，方向【正确】✅  不需要 --invert-tilt")
        elif delta > threshold:
            print("判定：tilt↑ → cy↑（目标在画面里往上移）")
            print("      = 正反馈，方向【反了】❌  需要 --invert-tilt")
        else:
            print("判定：Δcy=%.1f px 太小，无法判定" % delta)
            print("      可能：tilt 舵机没接/没转、目标在水平中心线上、人也在动")
            print("      先手动验证舵机：P 0 20 然后 P 0 -20，看摄像头有没有抬头低头")
    print("=" * 68)

    send("H", 1.0)
    ser.close()


if __name__ == "__main__":
    main()
