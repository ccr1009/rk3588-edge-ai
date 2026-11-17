#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""long_stability_preview.py — 长时间稳定性测试（纯预览模式，不碰舵机）

为什么用纯预览模式
------------------
用户要求深夜不要动舵机（吵到别人）。但纯预览模式（不带 --bridge）
**完全不驱动云台**，只跑流水线 + NPU + JPEG，性能指标与带云台时一致
（已实测验证：延迟 22.1ms 完全一样）。

所以这是唯一能在"不吵人"前提下做长时间稳定性验证的方法。

测什么
------
1. 启动纯预览模式的追踪程序（无 --bridge）
2. 每 30 秒采样一次 RSS / fps / npu_ms / 丢帧
3. 跑满指定时长（默认 20 分钟）
4. 分析：内存是否泄漏、帧率是否劣化、有无丢帧累积

判据
----
· RSS 增长 < 5MB        → 无内存泄漏
· fps 波动 < 2 FPS      → 帧率稳定
· 丢帧计数不增长        → 缓冲健康

用法
----
    python3 long_stability_preview.py --minutes 20
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

BIN = "/root/lab/w4_edgevision_gimbal_preview.final"
CAM = "/dev/video83"
LOG = "/tmp/long_stability.log"
SAMPLE_INTERVAL = 30      # 秒


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (r.stdout or "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20)
    args = ap.parse_args()

    total_sec = args.minutes * 60
    n_samples = int(total_sec / SAMPLE_INTERVAL)

    print("=" * 76)
    print(" 长时间稳定性测试（纯预览模式，不驱动舵机）")
    print("=" * 76)
    print(" 时长: %.0f 分钟, 采样 %d 次（每 %d 秒）"
          % (args.minutes, n_samples, SAMPLE_INTERVAL))

    # ---- 启动纯预览模式 ----
    print()
    print("▶ 启动追踪程序（无 --bridge，不碰云台）")
    logf = open("/tmp/preview_stab_run.log", "w")
    proc = subprocess.Popen(
        [BIN, CAM, "--no-3d"],
        cwd="/root/lab",
        stdout=logf,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    print("  PID: %d" % proc.pid)

    # 等程序起来
    ok = False
    for _ in range(30):
        time.sleep(1)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/status", timeout=2) as r:
                json.loads(r.read())
            ok = True
            break
        except Exception:
            if proc.poll() is not None:
                print(" ❌ 程序已退出，rc=%s" % proc.returncode)
                print(" 日志:")
                print(sh("tail -20 /tmp/preview_stab_run.log"))
                return 1
    if not ok:
        print(" ❌ 30 秒内 8080 未就绪")
        proc.kill()
        return 1
    print(" ✅ 程序就绪")

    # 确认确实是纯预览模式
    banner = sh("head -5 /tmp/preview_stab_run.log")
    if "纯预览模式" in banner:
        print(" ✅ 确认纯预览模式（不驱动云台）")
    else:
        print(" ⚠️ 未确认纯预览模式，检查横幅：")
        print(banner)

    # ---- 采样 ----
    print()
    print("▶ 开始采样")
    print("-" * 76)
    print(" %-10s %-8s %-8s %-10s %-8s %s"
          % ("时间", "RSS(MB)", "fps", "npu(ms)", "丢帧", "备注"))

    with open(LOG, "w") as f:
        f.write("time,rss_mb,fps,npu_ms,dropped,note\n")

    rss_samples = []
    fps_samples = []
    dropped_samples = []
    t_start = time.time()

    for i in range(n_samples):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/status", timeout=3) as r:
                j = json.loads(r.read())

            # 读 RSS
            rss = 0
            try:
                with open("/proc/%d/status" % proc.pid) as pf:
                    for line in pf:
                        if line.startswith("VmRSS:"):
                            rss = int(line.split()[1]) // 1024
                            break
            except Exception:
                pass

            note = ""
            if j["errors"] > 0:
                note += "errors=%d " % j["errors"]
            if j["reopens"] > 0:
                note += "reopens=%d " % j["reopens"]
            if j["badjpeg"] > 0:
                note += "badjpeg=%d " % j["badjpeg"]

            print(" %-10s %-8d %-8.2f %-10.2f %-8d %s"
                  % (time.strftime("%H:%M:%S"), rss, j["fps"],
                     j["npu_ms"], j["dropped"], note))

            with open(LOG, "a") as f:
                f.write("%s,%d,%.2f,%.2f,%d,%s\n"
                        % (time.strftime("%H:%M:%S"), rss, j["fps"],
                           j["npu_ms"], j["dropped"], note))

            rss_samples.append(rss)
            fps_samples.append(j["fps"])
            dropped_samples.append(j["dropped"])

        except Exception as e:
            print(" %-10s ERR: %s" % (time.strftime("%H:%M:%S"), e))
            with open(LOG, "a") as f:
                f.write("%s,ERR,%s\n" % (time.strftime("%H:%M:%S"), e))

        # 剩余时间（最后一次不睡）
        if i < n_samples - 1:
            time.sleep(SAMPLE_INTERVAL)

    elapsed = time.time() - t_start

    # ---- 停止程序 ----
    print()
    print("▶ 停止程序（SIGKILL，因为这个程序 SIGTERM 杀不死）")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()
    time.sleep(2)
    logf.close()

    # ---- 分析 ----
    print()
    print("=" * 76)
    print(" 分析结果")
    print("=" * 76)

    if not rss_samples:
        print(" ❌ 没有有效样本")
        return 1

    print(" 测试时长: %.1f 分钟（%d 个有效样本）" % (elapsed / 60.0, len(rss_samples)))
    print()

    # 内存
    rss_delta = rss_samples[-1] - rss_samples[0]
    rss_max = max(rss_samples)
    rss_min = min(rss_samples)
    print(" 【内存】")
    print("   起始 %d MB → 结束 %d MB  (Δ%+d MB)" % (rss_samples[0], rss_samples[-1], rss_delta))
    print("   范围 %d ~ %d MB" % (rss_min, rss_max))
    if abs(rss_delta) < 5:
        print("   ✅ 无内存泄漏（变化 < 5MB）")
        mem_ok = True
    elif rss_delta < 20:
        print("   ⚠️ 内存增长 %d MB，需观察" % rss_delta)
        mem_ok = False
    else:
        print("   ❌ 内存增长 %d MB，疑似泄漏" % rss_delta)
        mem_ok = False

    # 帧率
    print()
    print(" 【帧率】")
    fps_max = max(fps_samples)
    fps_min = min(fps_samples)
    fps_avg = sum(fps_samples) / len(fps_samples)
    print("   平均 %.2f FPS，范围 %.2f ~ %.2f" % (fps_avg, fps_min, fps_max))
    if fps_max - fps_min < 2.0:
        print("   ✅ 帧率稳定（波动 < 2 FPS）")
        fps_ok = True
    else:
        print("   ⚠️ 帧率波动 %.2f FPS" % (fps_max - fps_min))
        fps_ok = False

    # 丢帧
    print()
    print(" 【丢帧】")
    drop_delta = dropped_samples[-1] - dropped_samples[0]
    print("   起始 %d → 结束 %d  (Δ%+d)" % (dropped_samples[0], dropped_samples[-1], drop_delta))
    if drop_delta == 0:
        print("   ✅ 全程零丢帧")
        drop_ok = True
    else:
        print("   ⚠️ 累计丢帧 %d" % drop_delta)
        drop_ok = False

    # ---- 结论 ----
    print()
    print("=" * 76)
    all_ok = mem_ok and fps_ok and drop_ok
    if all_ok:
        print(" ✅ 长时间稳定性测试通过")
        print("    %.0f 分钟连续运行，内存无泄漏、帧率稳定、零丢帧" % (elapsed / 60.0))
        rc = 0
    else:
        print(" ⚠️ 部分指标未达标，详见上面各项")
        rc = 1
    print()
    print(" 日志: %s" % LOG)
    print(" 程序日志: /tmp/preview_stab_run.log")
    print("=" * 76)
    return rc


if __name__ == "__main__":
    sys.exit(main())
