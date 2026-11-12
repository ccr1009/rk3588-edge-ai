#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闭环现场验收：一边跑绝对映射追踪，一边记录 pan/tilt 轨迹，最后给出判据。

为什么单独写这个脚本而不是直接看 tracker 的滚动输出：
    滚动输出会刷屏，事后无法判断"云台到底有没有跟着目标动"。
    这里把每个控制指令都记下来，跑完给统计（pan 是否变化、是否跟随 dx、
    是否顶死限位、串口错误数），**用数字判断而不是用眼睛看**。

用法：
    python verify_closed_loop.py --port COM6 --host 10.21.3.103 --seconds 25
    python verify_closed_loop.py --port COM6 --host 10.21.3.103 --mode relative
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
from gimbal_control import (FRAME_SIZE, compute_control,
                            compute_control_absolute, parse_frame, pick_target)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--tcp-port", type=int, default=9000)
    ap.add_argument("--seconds", type=float, default=25.0)
    ap.add_argument("--mode", choices=["absolute", "relative"], default="absolute")
    ap.add_argument("--class", dest="want_class", default="person")
    ap.add_argument("--min-score", type=float, default=0.15)
    ap.add_argument("--fov-pan", type=float, default=60.0)
    ap.add_argument("--fov-tilt", type=float, default=36.0)
    ap.add_argument("--gimbal-fov-pan", type=float, default=120.0)
    ap.add_argument("--gimbal-fov-tilt", type=float, default=90.0)
    ap.add_argument("--camera-pan", type=float, default=0.0)
    ap.add_argument("--kp-pan", type=float, default=0.45)
    ap.add_argument("--kp-tilt", type=float, default=0.40)
    ap.add_argument("--deadzone", type=float, default=0.05)
    ap.add_argument("--max-step", type=float, default=6.0)
    ap.add_argument("--pan-range", type=float, default=90.0)
    ap.add_argument("--tilt-range", type=float, default=45.0)
    ap.add_argument("--invert-pan", action="store_true",
                    help="Pan 反号。判据：推云台右转，画面目标往左移=正常，往右移=要加")
    ap.add_argument("--invert-tilt", action="store_true",
                    help="Tilt 反号")
    # ── 目标位置滤波（抑制单帧误检，见下方注释）──────────────────
    ap.add_argument("--smooth", type=float, default=0.0,
                    help="目标位置一阶低通系数 alpha（0=关闭；0.35 约滞后 2~3 帧）。"
                         "单帧误检会把控制量带飞，开了更稳")
    ap.add_argument("--jump-max", type=float, default=0.25,
                    help="跳变抑制阈值（归一化）。单帧偏离滤波值超过它视为误检，丢弃该帧")
    args = ap.parse_args()

    # ---- 串口 ----
    try:
        ser = serial.Serial(args.port, 115200, timeout=0.3, write_timeout=2.0)
    except Exception as e:
        print("串口打不开: %s" % e)
        return 2
    ser.dtr = False
    ser.rts = False
    time.sleep(0.3)
    ser.reset_input_buffer()

    def send(cmd):
        try:
            ser.write((cmd + "\n").encode())
            ser.flush()
            return True
        except Exception:
            return False

    # ---- TCP ----
    try:
        sock = socket.create_connection((args.host, args.tcp_port), timeout=5)
    except Exception as e:
        print("连不上网关 %s:%d -> %s" % (args.host, args.tcp_port, e))
        ser.close()
        return 2

    send("H")            # 回中
    time.sleep(0.3)
    ser.reset_input_buffer()

    pan = tilt = 0.0
    cmds = 0
    errs = 0
    lost = 0
    frames = 0
    t0 = time.time()
    trace = []           # (t, frame_id, dx, dy, pan, tilt)
    last_print = 0.0

    # 一阶低通滤波状态（--smooth > 0 时才用）
    sm_cx = None
    sm_cy = None
    frames_jump = 0      # 被判为误检丢弃的帧数

    print("=" * 70)
    print("闭环现场验收  mode=%s  source=%s:%d  sink=%s  %.0fs"
          % (args.mode, args.host, args.tcp_port, args.port, args.seconds))
    print("=" * 70)

    try:
        while time.time() - t0 < args.seconds:
            raw = recv_frame(sock)
            if raw is None:
                continue
            try:
                fid, boxes = parse_frame(raw, 1280, 720)
            except Exception:
                continue
            frames += 1

            tgt = pick_target(boxes, args.want_class, args.min_score)
            if tgt is None:
                lost += 1
                continue

            # ── 目标位置低通滤波（关键）──────────────────────────
            # 不加这一层时实测会看到：dx 已经从 0.269 收敛到 0.069，
            # 但中间某一帧检测框突然跳到别处 → 控制量被带飞 → pan 又冲回 -90。
            # 单帧误检不应该影响云台，所以对 (cx, cy) 做一阶低通：
            #     new = alpha*measured + (1-alpha)*previous
            # alpha 越小越平滑、越滞后。云台转动本身不快（2.5°/帧），
            # alpha=0.35 时滞后约 2~3 帧（~150ms），可以接受。
            if args.smooth > 0:
                a = args.smooth
                if sm_cx is None:
                    sm_cx, sm_cy = tgt["cx"], tgt["cy"]
                else:
                    # ★ 跳变判定必须用【原始测量】和【滤波值】比，不能拿滤波后的值比。
                    #   滤波值天生就贴近上一帧，用它判断永远发现不了跳变（这是个真 bug）。
                    if abs(tgt["cx"] - sm_cx) > args.jump_max:
                        frames_jump += 1
                        continue
                    sm_cx = a * tgt["cx"] + (1 - a) * sm_cx
                    sm_cy = a * tgt["cy"] + (1 - a) * sm_cy
                cx_use, cy_use = sm_cx, sm_cy
            else:
                cx_use, cy_use = tgt["cx"], tgt["cy"]

            if args.mode == "absolute":
                npan, ntilt, in_dz, dx, dy = compute_control_absolute(
                    cx_use, cy_use, pan, tilt,
                    args.pan_range, args.tilt_range,
                    args.gimbal_fov_pan, args.gimbal_fov_tilt,
                    args.fov_pan, args.fov_tilt,
                    camera_pan=args.camera_pan,
                    deadzone=args.deadzone, max_step=args.max_step)
            else:
                npan, ntilt, in_dz, dx, dy = compute_control(
                    cx_use, cy_use, pan, tilt,
                    args.kp_pan, args.kp_tilt, args.fov_pan, args.fov_tilt,
                    args.pan_range, args.tilt_range,
                    args.deadzone, args.max_step,
                    invert_tilt=args.invert_tilt,
                    invert_pan=args.invert_pan)

            if not in_dz and (abs(npan - pan) > 0.05 or abs(ntilt - tilt) > 0.05):
                ok = send("P %.1f %.1f" % (npan, ntilt))
                if ok:
                    cmds += 1
                else:
                    errs += 1
                pan, tilt = npan, ntilt
                trace.append((time.time() - t0, fid, dx, dy, pan, tilt))

            if time.time() - last_print > 1.0:
                last_print = time.time()
                print("  %5.1fs frame=%-7d dx=%+.3f dy=%+.3f -> pan=%+6.1f tilt=%+5.1f"
                      % (time.time() - t0, fid, dx, dy, pan, tilt))
    except KeyboardInterrupt:
        print("\n[中断]")
    finally:
        send("H")
        time.sleep(0.2)
        ser.close()
        sock.close()

    # ---------------- 判据 ----------------
    print("\n" + "=" * 70)
    print("统计")
    print("=" * 70)
    elapsed = max(time.time() - t0, 1e-6)
    print("  运行 %.1fs  收到 %d 帧  下发 %d 条控制  串口错误 %d  无目标帧 %d"
          % (elapsed, frames, cmds, errs, lost))
    if frames_jump:
        print("  跳变帧（判为误检已丢弃）%d" % frames_jump)
    print("  控制频率 %.1f Hz" % (cmds / elapsed))

    if not trace:
        print("\n  ❌ 一条控制都没下发 —— 云台完全没动。")
        print("     可能：目标一直不在画面 / 置信度不够 / 检测一直是 0 个框")
        return 1

    pans = [t[4] for t in trace]
    tilts = [t[5] for t in trace]
    dxs = [t[2] for t in trace]

    print("\n  pan  范围 [%.1f, %.1f]  跨度 %.1f°  不同取值 %d 个"
          % (min(pans), max(pans), max(pans) - min(pans), len(set("%.1f" % p for p in pans))))
    print("  tilt 范围 [%.1f, %.1f]  跨度 %.1f°" % (min(tilts), max(tilts), max(tilts) - min(tilts)))
    print("  dx   范围 [%+.3f, %+.3f]" % (min(dxs), max(dxs)))

    ok_all = True

    if cmds >= 3 and (max(pans) - min(pans)) > 1.0:
        print("\n  ✅ 云台确实在动（pan 跨度 %.1f°）" % (max(pans) - min(pans)))
    else:
        print("\n  ❌ 云台几乎没动（pan 跨度 %.1f°）" % (max(pans) - min(pans)))
        ok_all = False

    if errs == 0:
        print("  ✅ 串口零错误")
    else:
        print("  ❌ 串口错误 %d 次" % errs)
        ok_all = False

    at_limit = sum(1 for p in pans if abs(abs(p) - args.pan_range) < 0.5)
    if at_limit == 0:
        print("  ✅ 没有顶死限位")
    else:
        print("  ⚠️  有 %d 次顶到限位（%.0f°）—— 目标可能真的在视野外" % (at_limit, args.pan_range))

    # 绝对映射模式下：pan 应该跟随 dx（画面右 → pan 正）
    if args.mode == "absolute" and len(trace) > 5:
        corr_num = sum((t[2]) * (t[4]) for t in trace)
        if corr_num > 0:
            print("  ✅ pan 方向与 dx 同向（目标偏右 → pan 增大）")
        else:
            print("  ⚠️  pan 方向与 dx 反号 —— 检查符号约定 / 是否该加 --camera-pan")

    print("\n" + "=" * 70)
    print("判定: %s" % ("✅ 闭环跑通" if ok_all else "❌ 有问题，见上面"))
    print("=" * 70)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
