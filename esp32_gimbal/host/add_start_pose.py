#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""add_start_pose.py — 给追踪程序加 --start-pan / --start-tilt 参数

需求（2026-11-17）
-----------------
用户把舵机固定了，但中位（P0 T0）朝天花板，看不到人。
程序启动时沿用当前姿态，如果当前姿态看不到人，
就会一直往错误方向追，最后顶到 P-90 限位卡死（实际发生过）。

解决：加两个参数，启动时先摆到指定姿态再开始追。

    ./v5_startpose /dev/video83 --bridge 127.0.0.1:9100 --no-3d \
        --start-pan 5 --start-tilt -20

实现要点
--------
1. 在 ControlCfg 加两个字段 start_pan / start_tilt，默认 NAN（表示不启用）
2. 参数解析加 --start-pan / --start-tilt
3. 云台连上之后，如果设了 start_pose，就发一次 P 命令摆过去，等它到位
4. 主循环里 cur_pan/cur_tilt 要初始化为这个值（否则第一帧控制会从 0 开始算）

注意：不能直接用 "goto" 跳转，要在云台初始化之后、主循环之前做。
"""
import sys

SRC = "/root/lab/w4_edgevision_gimbal_preview.cpp"

with open(SRC, encoding="utf-8") as f:
    src = f.read()

orig_len = len(src)
changes = []

# ============ 1. ControlCfg 加字段 ============
ANCHOR1 = """    float tilt_safe_min = -35.0f;"""
NEW1 = """    // ── 启动姿态（2026-11-17 新增）──────────────────────────────
    //
    // 为什么需要：
    //   舵机固定后，上电中位是 P0 T0（朝天花板），看不到人。
    //   程序启动时沿用当前姿态，如果看不到人就往错误方向一直追，
    //   最后顶到 P-90 限位卡死（实际发生过，用户反馈"停在那里动不了"）。
    //
    // 用法：
    //   --start-pan 5 --start-tilt -20
    //   启动时先摆到这个姿态再开始追踪。
    //
    // 默认 NAN = 不启用（沿用当前姿态，保持向后兼容）。
    float start_pan  = NAN;
    float start_tilt = NAN;

    float tilt_safe_min = -35.0f;"""

if ANCHOR1 not in src:
    print("❌ 锚点1未找到（tilt_safe_min）")
    sys.exit(1)
src = src.replace(ANCHOR1, NEW1, 1)
changes.append("ControlCfg 加 start_pan/start_tilt 字段")

# ============ 2. 参数解析 ============
ANCHOR2 = """        else if (strcmp(argv[i], "--deadzone-tilt") == 0 && i + 1 < argc) cfg.deadzone_tilt = atof(argv[++i]);"""
NEW2 = """        else if (strcmp(argv[i], "--deadzone-tilt") == 0 && i + 1 < argc) cfg.deadzone_tilt = atof(argv[++i]);
        // --start-pan / --start-tilt：启动时先摆到这个姿态（2026-11-17）
        //   解决"中位看不到人 → 往错误方向追到限位"的问题。
        else if (strcmp(argv[i], "--start-pan")  == 0 && i + 1 < argc) cfg.start_pan  = atof(argv[++i]);
        else if (strcmp(argv[i], "--start-tilt") == 0 && i + 1 < argc) cfg.start_tilt = atof(argv[++i]);"""

if ANCHOR2 not in src:
    print("❌ 锚点2未找到（--deadzone-tilt）")
    sys.exit(1)
src = src.replace(ANCHOR2, NEW2, 1)
changes.append("参数解析加 --start-pan / --start-tilt")

# ============ 3. 启动时摆姿态（两处：bridge 和 serial）============
# 在 bridge 分支的横幅打印之后插入
ANCHOR3 = """            printf("[i] 单帧位移上限: pan %.2f×42.7=%.1fpx | tilt %.2f×80.0=%.1fpx "
                   "（tilt 独立限幅，见 --max-step-tilt 注释）\\n",
                   cfg.max_step, cfg.max_step * 42.7f,
                   cfg.max_step_tilt, cfg.max_step_tilt * 80.0f);
        } else {
            std::cerr << "[!] 桥接服务连不上，退化为纯预览模式\\n";"""

NEW3 = """            printf("[i] 单帧位移上限: pan %.2f×42.7=%.1fpx | tilt %.2f×80.0=%.1fpx "
                   "（tilt 独立限幅，见 --max-step-tilt 注释）\\n",
                   cfg.max_step, cfg.max_step * 42.7f,
                   cfg.max_step_tilt, cfg.max_step_tilt * 80.0f);

            // ── 启动姿态（2026-11-17）──────────────────────────────
            //   如果指定了 --start-pan/--start-tilt，先摆过去再开始追。
            //   为什么需要：舵机固定后中位可能看不到人，不摆过去就会往错误方向
            //   一直追、顶到限位卡死（用户实际遇到：P-90 卡住动不了）。
            if (!isnan(cfg.start_pan) || !isnan(cfg.start_tilt)) {
                float sp = isnan(cfg.start_pan)  ? 0.0f : cfg.start_pan;
                float st = isnan(cfg.start_tilt) ? 0.0f : cfg.start_tilt;
                char cmd[64];
                int n = snprintf(cmd, sizeof(cmd), "P %.2f %.2f\\n", sp, st);
                if (write(gimbal.fd_for_start(), cmd, n) > 0) {
                    printf("[+] 启动姿态: P %+.1f %+.1f（等待到位）\\n", sp, st);
                    usleep(1500000);      // 等舵机转过去（1.5s 够走满行程）
                }
            }
        } else {
            std::cerr << "[!] 桥接服务连不上，退化为纯预览模式\\n";"""

if ANCHOR3 not in src:
    print("❌ 锚点3未找到（bridge 分支横幅）")
    sys.exit(1)
src = src.replace(ANCHOR3, NEW3, 1)
changes.append("bridge 分支启动时摆姿态")

# ============ 4. GimbalSerial 加 fd_for_start() 访问器 ============
ANCHOR4 = """    // 主循环调用：只更新目标，不做 I/O
    void set_target(float pan, float tilt) {"""
NEW4 = """    // 给启动姿态用的裸 fd（2026-11-17）
    //   为什么需要：启动时要直接发一条 P 命令摆姿态，但 set_target 是异步的
    //   （只写目标、由串口线程发），没法确认"发出去"和"到位"。
    //   这里暴露 fd 让 main 直接写一条并等待。
    int fd_for_start() const { return fd_; }

    // 主循环调用：只更新目标，不做 I/O
    void set_target(float pan, float tilt) {"""

if ANCHOR4 not in src:
    print("❌ 锚点4未找到（set_target）")
    sys.exit(1)
src = src.replace(ANCHOR4, NEW4, 1)
changes.append("GimbalSerial 加 fd_for_start()")

# ============ 5. 确保 cmath 被 include（isnan）============
if "#include <cmath>" not in src:
    for anchor in ("#include <cstdio>", "#include <stdio.h>"):
        if anchor in src:
            src = src.replace(anchor, "#include <cmath>\n" + anchor, 1)
            changes.append("补 #include <cmath>")
            break

# ============ 6. 用法说明 ============
ANCHOR6 = """//      --deadzone-tilt N    tilt 死区（整幅高归一化），默认 0.035 → 0.035×720 = 25.2 px"""
NEW6 = """//      --deadzone-tilt N    tilt 死区（整幅高归一化），默认 0.035 → 0.035×720 = 25.2 px
//      --start-pan N        ★ 启动姿态 pan（2026-11-17 新增）
//      --start-tilt N       ★ 启动姿态 tilt
//                           为什么需要：舵机固定后中位可能看不到人，
//                           不先摆过去就会往错误方向追、顶到限位卡死。"""

if ANCHOR6 in src:
    src = src.replace(ANCHOR6, NEW6, 1)
    changes.append("补用法说明")

with open(SRC, "w", encoding="utf-8") as f:
    f.write(src)

print("=" * 64)
print(" 改动清单：")
for c in changes:
    print("   ✓ " + c)
print()
print(" 源码：%d → %d 字节（+%d）" % (orig_len, len(src), len(src) - orig_len))
print("=" * 64)
