#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_p1_slew.py — 验证 S 40 帧间插值是否真的生效

原理
----
固件的 S 命令设置缓动时长（g_slew_ms）。启用后 g_pan 不再瞬间跳到目标，
而是在 20ms 控制循环里指数逼近，所以 Q 命令会读到**中间值**。

对比：
  S 0    → 发 P 40 0，立刻 Q 应该就读到 pan=40
  S 1000 → 发 P 40 0，立刻 Q 读到的是 0~40 之间的中间值，要等约 1 秒才到位

如果能观察到中间值，说明 S 生效。
"""
import socket
import time


def cmd(s, c, wait=0.5):
    s.sendall((c + "\n").encode())
    s.settimeout(wait)
    buf = b""
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            ch = s.recv(1)
        except socket.timeout:
            break
        if not ch or ch == b"\n":
            break
        buf += ch
    return buf.decode("utf-8", "replace").strip()


def parse_pos(reply):
    """从 'S <pan> <tilt> <ready>' 或 'OK <pan> <tilt>' 里取 (pan, tilt)。"""
    if not reply:
        return None
    p = reply.split()
    if len(p) >= 3 and p[0] in ("S", "OK"):
        try:
            return float(p[1]), float(p[2])
        except ValueError:
            return None
    return None


def main():
    s = socket.socket()
    s.settimeout(2.0)
    s.connect(("127.0.0.1", 9100))

    print("=" * 70)
    print(" 验证 S（缓动/帧间插值）是否生效")
    print("=" * 70)
    print(" 引脚: %s" % cmd(s, "I", 1.0))

    # ---- 测试 A：S 0（无缓动）----
    print()
    print("▶ A. S 0（无缓动）：发 P 40 0 后立刻查询")
    print("-" * 70)
    cmd(s, "S 0", 0.6)
    cmd(s, "P 0 -20", 0.6)
    time.sleep(1.0)
    cmd(s, "P 0 -20", 0.6)   # 确保在位

    cmd(s, "P 40 -20", 0.4)
    for i in range(5):
        q = cmd(s, "Q", 0.3)
        pos = parse_pos(q)
        print("   采样 %d: %-22s → pan=%s" %
              (i + 1, q, ("%.1f" % pos[0]) if pos else "?"))
        time.sleep(0.1)

    # ---- 测试 B：S 1000（强缓动）----
    print()
    print("▶ B. S 1000（1 秒缓动）：同样发 P 40 0，应该看到中间值")
    print("-" * 70)
    cmd(s, "S 1000", 0.6)
    cmd(s, "P 0 -20", 1.5)
    time.sleep(1.8)

    cmd(s, "P 40 -20", 0.4)
    samples = []
    for i in range(10):
        q = cmd(s, "Q", 0.25)
        pos = parse_pos(q)
        v = pos[0] if pos else None
        samples.append(v)
        print("   采样 %d: %-22s → pan=%s" %
              (i + 1, q, ("%.1f" % v) if v is not None else "?"))
        time.sleep(0.12)

    # ---- 判定 ----
    print()
    print("=" * 70)
    valid = [v for v in samples if v is not None]
    if len(valid) >= 3:
        mid = [v for v in valid if 2.0 < v < 38.0]
        if mid:
            print(" ✅ S 生效：观察到 %d 个中间值 %s"
                  % (len(mid), ", ".join("%.1f" % v for v in mid[:6])))
            print("    说明 pan 是逐步逼近 40 的，不是瞬间跳过去 → 帧间插值工作正常")
        else:
            print(" ⚠️ 没观察到中间值：%s" % ", ".join("%.1f" % v for v in valid))
            print("    可能是采样间隔太大（缓动太快），或 S 命令没生效")
    else:
        print(" ⚠️ 采样失败，无法判定")

    # 恢复
    cmd(s, "S 0", 0.5)
    cmd(s, "P 0 -20", 0.6)
    s.close()
    print()
    print(" 已恢复 S 0（注意：程序下次启动会重新发 S 40）")
    print("=" * 70)


if __name__ == "__main__":
    main()
