#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stability_monitor.py — 10 分钟稳定性采样

记录 RSS / fps / npu_ms / errors / dropped，用于证明长时间运行不泄漏、不劣化。
"""
import json
import subprocess
import time
import urllib.request

LOG = "/tmp/stability2.log"
N = 20          # 采样次数
INTERVAL = 30   # 秒


def get_pid():
    r = subprocess.run(["pgrep", "-f", "w4_edgevision_gimbal_preview.final"],
                       capture_output=True, text=True)
    pids = (r.stdout or "").strip().split()
    return pids[0] if pids else None


def get_rss_mb(pid):
    if not pid:
        return 0
    try:
        with open("/proc/%s/status" % pid) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def main():
    with open(LOG, "w") as f:
        f.write("time,rss_mb,fps,npu_ms,errors,dropped,reopens,badjpeg\n")

    for i in range(N):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/status", timeout=3) as r:
                j = json.loads(r.read())
            pid = get_pid()
            rss = get_rss_mb(pid)
            row = "%s,%d,%.2f,%.2f,%d,%d,%d,%d" % (
                time.strftime("%H:%M:%S"), rss, j["fps"], j["npu_ms"],
                j["errors"], j["dropped"], j["reopens"], j["badjpeg"])
            print(row, flush=True)
            with open(LOG, "a") as f:
                f.write(row + "\n")
        except Exception as e:
            print("ERR:", e, flush=True)
            with open(LOG, "a") as f:
                f.write("%s,ERR,%s\n" % (time.strftime("%H:%M:%S"), e))
        time.sleep(INTERVAL)

    with open(LOG, "a") as f:
        f.write("DONE\n")
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
