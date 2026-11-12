#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动确认：云台转的时候，摄像头到底跟不跟着转？

不需要检测、不需要目标 —— 只发角度指令，让人用眼睛看。
这是最原始也最可靠的验证：数据可以骗人，眼睛不会。

用法:
    python check_mounting.py --port COM6
    python check_mounting.py --port COM6 --no-move   # 只读状态不动
"""
import argparse
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

try:
    import serial
except ImportError:
    print("缺 pyserial", file=sys.stderr)
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM6")
    ap.add_argument("--no-move", action="store_true")
    args = ap.parse_args()

    s = serial.Serial(args.port, 115200, timeout=0.6, write_timeout=2.0)
    s.dtr = False
    s.rts = False
    time.sleep(0.3)
    s.reset_input_buffer()

    def cmd(c, w=0.5):
        s.write((c + "\n").encode())
        s.flush()
        time.sleep(w)
        n = s.in_waiting
        txt = (s.read(n) if n else b"").decode("utf-8", "replace")
        # 过滤启动日志
        lines = [l for l in txt.splitlines()
                 if l.strip() and not l.strip().startswith("I (")]
        return " | ".join(lines) if lines else "(无响应)"

    print("=" * 66)
    print("摄像头是否随云台转 —— 用眼睛看")
    print("=" * 66)
    print()
    print("当前状态: %s" % cmd("I"))
    print("查询角度: %s" % cmd("Q"))

    if args.no_move:
        s.close()
        return 0

    print()
    print("接下来云台会转 4 次。每次停下来后请回答：")
    print("  ★ 摄像头本体（那个小方块镜头）跟着转向了吗？")
    print()

    steps = [
        (45.0, 0.0, "pan 转到 +45°（应该往右转）"),
        (-45.0, 0.0, "pan 转到 -45°（应该往左转）"),
        (0.0, 30.0, "tilt 转到 +30°（应该抬头）"),
        (0.0, -30.0, "tilt 转到 -30°（应该低头）"),
    ]

    for pan, tilt, desc in steps:
        print("-" * 66)
        print("%s" % desc)
        print("  发: P %.0f %.0f" % (pan, tilt))
        r = cmd("P %.0f %.0f" % (pan, tilt), w=1.5)
        print("  收: %s" % r)
        print("  读回: %s" % cmd("Q", w=0.4))
        print()
        input("  看完了按回车继续 ... ")

    print("-" * 66)
    print("回中: %s" % cmd("H", w=1.0))
    s.close()

    print()
    print("=" * 66)
    print("请回答：")
    print("  ① 4 次转动时，摄像头本体都跟着动了吗？")
    print("  ② 哪几次动了、哪几次没动？")
    print()
    print("判读：")
    print("  4 次都动 → 摄像头固定正确，问题在控制参数")
    print("  只动 1~2 次 → 某一轴没连上（tilt 平台没固定）")
    print("  都不动 → 摄像头装在了底座，不随云台动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
