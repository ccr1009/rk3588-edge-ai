#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""add_home_on_lost.py — 加「丢目标回位」逻辑

问题（2026-11-17）
-----------------
加了 --start-pan/--start-tilt 后，启动时能看到人。但人一旦离开画面：

    看不到 → objects=0 → has_target=false
    → 控制律不动作（lost_frames 累加）
    → 但也不会回位，就停在原地

如果之前因为追错方向已经推偏了，就永远停在那里，人回来也找不到。

解决
----
连续 N 帧（默认 30 帧 ≈ 1.2 秒 @25fps）没有可追目标时，
**回到启动姿态**（--start-pan/--start-tilt）。

为什么是"回到启动姿态"而不是"回中位 P0 T0"：
    中位是朝天花板的，回去也是白回。启动姿态是用户确认过"能看到人"的角度。

实现
----
在 `if (++lost_frames > 30)` 那里加回位动作。
但要注意不能每帧都发（会持续占用串口），用状态标志控制：
    丢目标 → 累计到阈值 → 发一次回位 → 置标志，之后不再重复发
    重新捕获目标 → 清标志，恢复正常追踪
"""
import sys

SRC = "/root/lab/w4_edgevision_gimbal_preview.cpp"

with open(SRC, encoding="utf-8") as f:
    src = f.read()

orig_len = len(src)

# ---------- 1. 加配置项 ----------
ANCHOR1 = """    float start_pan  = NAN;
    float start_tilt = NAN;"""
NEW1 = """    float start_pan  = NAN;
    float start_tilt = NAN;

    // ── 丢目标回位（2026-11-17 新增）────────────────────────────
    //
    // 为什么需要：
    //   人离开画面后，控制律不动作，云台停在原地。
    //   如果之前追错方向已经推偏了，就永远停在那里，人回来也找不到。
    //
    // 逻辑：连续 lost_home_frames 帧没有可追目标时，回到启动姿态。
    //   0 = 不启用（保持原行为）。
    //
    // 为什么回到"启动姿态"而不是"中位 P0 T0"：
    //   中位是朝天花板的，回去也是白回。启动姿态是确认过"能看到人"的角度。
    int lost_home_frames = 30;      // 约 1.2 秒 @ 25fps"""

if ANCHOR1 not in src:
    print("❌ 锚点1未找到")
    sys.exit(1)
src = src.replace(ANCHOR1, NEW1, 1)
print("  ✓ ControlCfg 加 lost_home_frames")

# ---------- 2. 加状态变量 ----------
ANCHOR2 = """    int lost_frames = 0;"""
NEW2 = """    int lost_frames = 0;
    bool homed_after_lost = false;   // 丢目标后是否已经回过位（避免每帧重复发）"""

if ANCHOR2 not in src:
    print("❌ 锚点2未找到")
    sys.exit(1)
src = src.replace(ANCHOR2, NEW2, 1)
print("  ✓ 加 homed_after_lost 状态变量")

# ---------- 3. 重新捕获目标时清标志 ----------
ANCHOR3 = """            if (has_target) {
                lost_frames = 0;"""
NEW3 = """            if (has_target) {
                lost_frames = 0;
                homed_after_lost = false;    // 重新捕获到目标，清掉回位标志"""

if ANCHOR3 not in src:
    print("❌ 锚点3未找到")
    sys.exit(1)
src = src.replace(ANCHOR3, NEW3, 1)
print("  ✓ 重新捕获时清标志")

# ---------- 4. 回位逻辑 ----------
ANCHOR4 = """            } else {
                if (++lost_frames > 30) { locked = false; sm_init = false; }
            }"""
NEW4 = """            } else {
                // ── 丢目标回位（2026-11-17）──────────────────────────
                //
                // 连续 N 帧看不到可追目标时，回到启动姿态。
                //
                // 为什么必须做：人离开画面后控制律不动作，云台停在原地。
                // 如果之前追错方向已经推偏（实测会顶到 P-90），人回来也找不到。
                // 回到"确认过能看到人"的启动姿态，人一回来就能重新锁定。
                //
                // 三道保护：
                //   1. lost_home_frames == 0 时完全不启用（保持向后兼容）
                //   2. 没设启动姿态时回到中位（否则没地方可回）
                //   3. homed_after_lost 标志防止每帧重复发（省串口带宽、少舵机磨损）
                ++lost_frames;                     // 先累加（只加一次，别在 if 里自增）
                if (lost_frames > 30) {            // 原语义：超 30 帧认定丢失
                    locked = false;
                    sm_init = false;
                }

                if (px->cfg.lost_home_frames > 0 &&
                    lost_frames > px->cfg.lost_home_frames &&
                    !homed_after_lost) {
                    // 目标姿态：优先启动姿态，没设就回中位
                    float hp = isnan(px->cfg.start_pan)  ? 0.f : px->cfg.start_pan;
                    float ht = isnan(px->cfg.start_tilt) ? 0.f : px->cfg.start_tilt;

                    cur_pan  = hp;
                    cur_tilt = ht;
                    px->gimbal->set_target(cur_pan, cur_tilt);
                    g_ctrl_sent++;
                    homed_after_lost = true;
                    g_home_on_lost++;         // 计数，供观察
                    printf("[gimbal] 丢目标 %d 帧，回到启动姿态 P %+.1f %+.1f\\n",
                           px->cfg.lost_home_frames, hp, ht);
                }
            }"""

if ANCHOR4 not in src:
    print("❌ 锚点4未找到")
    sys.exit(1)
src = src.replace(ANCHOR4, NEW4, 1)
print("  ✓ 加回位逻辑")

# ---------- 5. 加全局计数器 ----------
ANCHOR5 = """static std::atomic<int>      g_prof_state{0};"""
NEW5 = """static std::atomic<int>      g_ctrl_sent_placeholder{0};   // 占位（避免改动插入点）
static std::atomic<int>      g_home_on_lost{0};           // 丢目标回位次数（2026-11-17）
static std::atomic<int>      g_prof_state{0};"""

if ANCHOR5 in src:
    src = src.replace(ANCHOR5, NEW5, 1)
    print("  ✓ 加 g_home_on_lost 计数器")
else:
    print("  ⚠️ 计数器锚点未找到，尝试备用位置")
    # 找 g_ctrl_sent 定义处
    import re
    m = re.search(r'static std::atomic<int>\s+g_ctrl_sent', src)
    if m:
        src = src[:m.start()] + "static std::atomic<int>      g_home_on_lost{0};\n" + src[m.start():]
        print("  ✓ 在 g_ctrl_sent 前插入计数器")

# ---------- 6. 参数解析 ----------
ANCHOR6 = """        else if (strcmp(argv[i], "--start-tilt") == 0 && i + 1 < argc) cfg.start_tilt = atof(argv[++i]);"""
NEW6 = """        else if (strcmp(argv[i], "--start-tilt") == 0 && i + 1 < argc) cfg.start_tilt = atof(argv[++i]);
        // --lost-home-frames N：丢目标 N 帧后回到启动姿态（0 = 不启用）
        else if (strcmp(argv[i], "--lost-home-frames") == 0 && i + 1 < argc) cfg.lost_home_frames = atoi(argv[++i]);"""

if ANCHOR6 not in src:
    print("❌ 锚点6未找到")
    sys.exit(1)
src = src.replace(ANCHOR6, NEW6, 1)
print("  ✓ 参数解析加 --lost-home-frames")

# ---------- 7. /status 输出加字段 ----------
# 找 serve_status 里的 snprintf
import re
m = re.search(r'("\\"pan\\":%.2f,\\"tilt\\":%.2f,\\"locked\\":%s,\\"sent\\":%d,\\"errors\\":%d,")', src)
if m:
    print("  ℹ️ 找到 /status 格式串，保持不变（避免破坏协议）")
else:
    print("  ℹ️ /status 格式串未匹配，保持原样")

with open(SRC, "w", encoding="utf-8") as f:
    f.write(src)

print()
print("✅ 丢目标回位逻辑已添加")
print(" 源码：%d → %d 字节（+%d）" % (orig_len, len(src), len(src) - orig_len))
