#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_thread_leak.py — 验证流水线线程不会泄漏

背景
----
P4 加了 3 个线程（采集/推理/显示）。长时间运行或反复启停时，
如果有线程没 join 干净，会出现：
  · 进程退不出（僵尸）
  · 端口 8080/9000 被占
  · 内存缓慢上涨

本脚本检查：
  1. 当前线程数是否稳定（不随时间增长）
  2. 服务重启后端口是否正常释放
  3. 连续启停 3 次，每次都能正常起来

用法
----
    python3 check_thread_leak.py
"""
import json
import subprocess
import sys
import time
import urllib.request


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (r.stdout or "").strip()


def get_pid():
    pids = sh("pgrep -f 'w4_edgevision_gimbal_preview.final'").split()
    return pids[0] if pids else None


def thread_count(pid):
    if not pid:
        return -1
    return int(sh("ls /proc/%s/task 2>/dev/null | wc -l" % pid) or -1)


def rss_mb(pid):
    if not pid:
        return -1
    out = sh("grep VmRSS /proc/%s/status 2>/dev/null" % pid)
    if not out:
        return -1
    return int(out.split()[1]) // 1024


def port_listening(port):
    return bool(sh("ss -ltn | grep -c ':%d '" % port).strip() not in ("0", ""))


def status_ok():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/status", timeout=3) as r:
            j = json.loads(r.read())
            return j
    except Exception:
        return None


def main():
    print("=" * 74)
    print(" 流水线线程/内存泄漏检查")
    print("=" * 74)

    # ---- 1. 线程数稳定性 ----
    print()
    print("▶ 1. 线程数与内存（60 秒采样，每 15 秒）")
    print("-" * 74)
    print(" %-10s %-8s %-10s %-10s %s" % ("时间", "PID", "线程数", "RSS(MB)", "fps"))
    baseline_threads = None
    samples = []
    for i in range(5):
        pid = get_pid()
        tc = thread_count(pid)
        rss = rss_mb(pid)
        j = status_ok()
        fps = j["fps"] if j else 0
        if baseline_threads is None:
            baseline_threads = tc
        print(" %-10s %-8s %-10d %-10d %.1f"
              % (time.strftime("%H:%M:%S"), pid or "-", tc, rss, fps))
        samples.append((tc, rss))
        time.sleep(15)

    threads_stable = len({s[0] for s in samples}) == 1
    rss_delta = samples[-1][1] - samples[0][1]
    print()
    if threads_stable:
        print(" ✅ 线程数恒定 %d，无泄漏" % baseline_threads)
    else:
        print(" ⚠️ 线程数变化：%s" % [s[0] for s in samples])
    if abs(rss_delta) <= 3:
        print(" ✅ 内存稳定（%+d MB）" % rss_delta)
    elif rss_delta > 10:
        print(" ❌ 内存增长 %+d MB，疑似泄漏" % rss_delta)
    else:
        print(" ⚠️ 内存波动 %+d MB，需观察" % rss_delta)

    # ---- 2. 重启测试 ----
    print()
    print("▶ 2. 连续启停 3 次，检查端口释放")
    print("-" * 74)
    ok_count = 0
    for i in range(3):
        print("   第 %d 次..." % (i + 1), end="", flush=True)

        sh("systemctl stop gimbal-tracker")
        time.sleep(3)
        p8080 = port_listening(8080)
        p9000 = port_listening(9000)
        if p8080 or p9000:
            print(" ❌ 停止后端口未释放 (8080=%s 9000=%s)" % (p8080, p9000))
            continue

        sh("systemctl start gimbal-tracker")
        time.sleep(10)
        j = status_ok()
        if j is None:
            print(" ❌ 启动后 8080 无响应")
            continue

        pid = get_pid()
        tc = thread_count(pid)
        print(" ✅ 线程=%d fps=%.1f errors=%d"
              % (tc, j["fps"], j["errors"]))
        ok_count += 1

    print()
    if ok_count == 3:
        print(" ✅ 3/3 启停正常，端口完全释放，线程无残留")
    else:
        print(" ⚠️ %d/3 正常" % ok_count)

    # ---- 汇总 ----
    print()
    print("=" * 74)
    print(" 结论")
    print("=" * 74)
    print("  线程稳定：%s" % ("是" if threads_stable else "否"))
    print("  内存稳定：%s（%+d MB）" % ("是" if abs(rss_delta) <= 3 else "否", rss_delta))
    print("  启停正常：%d/3" % ok_count)
    print("=" * 74)
    return 0 if (threads_stable and abs(rss_delta) <= 10 and ok_count == 3) else 1


if __name__ == "__main__":
    sys.exit(main())
