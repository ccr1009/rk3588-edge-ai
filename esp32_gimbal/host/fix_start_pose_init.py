#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_start_pose_init.py — 修启动姿态的初始化漏项

问题（2026-11-17 实测）
----------------------
加了 --start-pan/--start-tilt 后，日志显示姿态摆过去了：
    [+] 启动姿态: P +5.0 -20.0（等待到位）

但下一帧控制立刻把它拉回来了：
    P-1.6 T-5.0 LOST      ← tilt 从 -20 被拉到 -5（tilt_safe 上界）

根因：thread_b_infer 里 `float cur_pan = 0.f, cur_tilt = 0.f;`
     控制律用的是 cur_pan/cur_tilt（当前角度），它被初始化成 0，
     而不是启动姿态。所以第一帧算出"当前在 T0，目标应该在 T-20"，
     但 tilt_safe=[-35,-5] 把结果钳到 -5。

     实际现象就是：姿态摆到 -20 了，但程序以为在 0，于是往错误方向修正。

修法
----
把 cur_pan/cur_tilt 初始化为启动姿态（如果指定了的话）。
"""
import sys

SRC = "/root/lab/w4_edgevision_gimbal_preview.cpp"

with open(SRC, encoding="utf-8") as f:
    src = f.read()

OLD = """    // 云台当前角度：跟踪**最后一次确认**的角度，不是想要的角度（沿用原语义）
    float cur_pan = 0.f, cur_tilt = 0.f;"""

NEW = """    // 云台当前角度：跟踪**最后一次确认**的角度，不是想要的角度（沿用原语义）
    //
    // ★ 2026-11-17 修正：初值必须等于**启动姿态**，不能写死 0。
    //   踩过的坑：加了 --start-pan/--start-tilt 后，姿态确实摆过去了
    //   （日志有 "[+] 启动姿态: P +5.0 -20.0"），但控制律以为当前还在 (0,0)，
    //   第一帧就按"从 0 出发"算增量，结果 tilt 被 tilt_safe 钳到 -5：
    //       P-1.6 T-5.0 LOST     ← 摆到 -20 了却往 -5 修正
    //   所以这里要跟着启动姿态走，否则白摆。
    float cur_pan  = isnan(px->cfg.start_pan)  ? 0.f : px->cfg.start_pan;
    float cur_tilt = isnan(px->cfg.start_tilt) ? 0.f : px->cfg.start_tilt;"""

if OLD not in src:
    print("❌ 锚点未找到")
    # 找找实际文本
    import re
    m = re.search(r'float cur_pan = 0\.f, cur_tilt = 0\.f;', src)
    if m:
        start = max(0, m.start() - 300)
        print("附近内容：")
        print(src[start:m.end() + 100])
    sys.exit(1)

src = src.replace(OLD, NEW, 1)

# 确认 PipelineCtx 里有 cfg 成员
if "cfg" not in src[src.find("struct PipelineCtx"):src.find("struct PipelineCtx") + 2000]:
    print("⚠️ PipelineCtx 可能没有 cfg 成员，检查一下")

with open(SRC, "w", encoding="utf-8") as f:
    f.write(src)

print("✅ 启动姿态初始化已修正")
