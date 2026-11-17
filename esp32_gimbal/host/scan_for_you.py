#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan_for_you.py — 转一圈找你在哪

背景
----
用户把舵机固定了，但朝向不对（中位看天花板）。
用户没法过去调整（靠墙），只能靠云台转过去找人。

扫法
----
pan 从 -85 到 +85 逐档扫，每档几个 tilt（-35 ~ -5），
每档抓帧检测 person，找到就记下来。

⚠️ 会驱动舵机（转一圈约 2 分钟）
"""
import re
import socket
import subprocess
import sys
import time

import cv2

BRIDGE = ("127.0.0.1", 9100)
CAM = 83
BIN = "/root/edgevision/deploy/cpp/build_release/edgevision_pro_rknn"
CWD = "/root/edgevision"
PAT = re.compile(r"^\s*(\d+)\s+(\w+)\s+([\d.]+)\s+\(\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)")


class G:
    def __init__(self):
        self.s = socket.socket()
        self.s.settimeout(2.0)
        self.s.connect(BRIDGE)

    def cmd(self, c, t=1.2):
        self.s.sendall((c + "\n").encode())
        self.s.settimeout(t)
        buf = b""
        t0 = time.time()
        while time.time() - t0 < t:
            try:
                ch = self.s.recv(1)
            except socket.timeout:
                break
            if not ch or ch == b"\n":
                break
            buf += ch
        return buf.decode("utf-8", "replace").strip()

    def set(self, p, t):
        return self.cmd("P %.1f %.1f" % (p, t))

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def detect(jpg):
    r = subprocess.run([BIN, jpg], cwd=CWD, capture_output=True, text=True, timeout=60)
    out = []
    for line in (r.stdout or "").splitlines():
        m = PAT.match(line)
        if m and int(m.group(1)) == 0:
            out.append((float(m.group(3)),
                        (int(m.group(4)), int(m.group(5)),
                         int(m.group(6)), int(m.group(7)))))
    return out


def main():
    # pan 粗扫，tilt 从 -35 到 -5（往下压才可能看到人）
    pans = list(range(-85, 90, 15))
    tilts = [-35, -25, -15, -5]

    g = G()
    print("=" * 84)
    print(" 转一圈找你在哪（约 %d 个角度组合）" % (len(pans) * len(tilts)))
    print("=" * 84)
    print(" 引脚:", g.cmd("I"))
    print(" pan:", pans)
    print(" tilt:", tilts)
    print()
    print(" pan  tilt   检出")
    print("-" * 84)

    cap = cv2.VideoCapture(CAM, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    if not cap.isOpened():
        print("❌ 打不开相机（追踪程序可能还占着）")
        return 1
    for _ in range(12):
        cap.read()

    hits = []
    total = len(pans) * len(tilts)
    n = 0

    for tilt in tilts:
        for pan in pans:
            n += 1
            g.set(pan, tilt)
            time.sleep(0.8)
            for _ in range(2):
                cap.read()
            ok, f = cap.read()
            if not ok or f is None:
                continue

            p = "/tmp/scan_%+04d_%+04d.jpg" % (pan, tilt)
            cv2.imwrite(p, f)
            ds = detect(p)

            if not ds:
                print(" %+4d %+4d   -                                   [%d/%d]"
                      % (pan, tilt, n, total))
                continue

            conf, (x1, y1, x2, y2) = max(ds, key=lambda d: d[0])
            area = (x2 - x1) * (y2 - y1) / float(1280 * 720) * 100
            head_ok = y1 > 14 and x1 > 25 and x2 < 1255
            tag = "✅可追" if head_ok else ("头切" if y1 <= 14 else "侧裁")
            print(" %+4d %+4d   person %.2f 面积%5.1f%% 框(%4d,%4d,%4d,%4d)  %s  [%d/%d]"
                  % (pan, tilt, conf, area, x1, y1, x2, y2, tag, n, total))
            hits.append((pan, tilt, conf, area, head_ok, p))

    g.set(0, 0)
    g.close()
    cap.release()

    print()
    print("=" * 84)
    if hits:
        good = [h for h in hits if h[4]]
        print(" 检出 person %d 次，其中可追 %d 次" % (len(hits), len(good)))
        print()
        if good:
            good.sort(key=lambda h: -h[2])
            print(" ★ 可追的角度（按置信度）：")
            for pan, tilt, conf, area, _, p in good[:10]:
                print("   P %+4d %+4d   conf=%.2f  面积=%.1f%%   %s"
                      % (pan, tilt, conf, area, p))
            best = good[0]
            print()
            print(" ★★ 推荐：P %+d %+d" % (best[0], best[1]))
        else:
            print(" ⚠️ 能检出人但全部贴边/头切，需要调 tilt")
            for pan, tilt, conf, area, _, p in sorted(hits, key=lambda h: -h[2])[:6]:
                print("   P %+4d %+4d  conf=%.2f 面积=%.1f%%  %s" % (pan, tilt, conf, area, p))
    else:
        print(" ❌ 全角度都没检出 person")
        print("    → 你可能不在镜头能看到的范围内，或者被挡住了")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    sys.exit(main())
