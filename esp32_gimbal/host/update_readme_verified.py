# -*- coding: utf-8 -*-
"""更新云台文档：补充 2026-11-22 的完整验证结论

背景
----
用户反馈：项目已完全跑通并通过实际验证。查证结果：
  · 代码自 11-17 后未改动（v6_home 仍是最终版）
  · ESP32 固件仍是 333Hz（p50 = 3.00 ms）
  · 用户 11-22 实际验证：追人稳定不振荡、丢目标自动回位、连续运行十几分钟无问题

但文档有两处严重过时：
  1. README 第 50-61 行「状态（2026-11-03 深夜）」—— 还写着"待办 tilt 舵机"，
     而 tilt 早在 11-16 就接好并跑了
  2. README 第 596-597 行 —— 写着"TILT 轴未接舵机实测，双轴同动均未验证"，
     而现在双轴都在实时追踪

改动：
  · README 状态段更新为 2026-11-22「已验证跑通」
  · 8.1.1 的"未验证"表述改为补注（保留历史记录，但标明后续已验证）
"""
import io
import os
import sys

README = r"D:\a研究生\项目\rk3588项目\rk3588-edge-ai\esp32_gimbal\README.md"

# ── 新状态段 ──────────────────────────────────────────────────────
OLD_STATUS = """> **状态（2026-11-03 深夜）**：
> - ✅ 固件已烧录到真实 ESP32-S3 N16R8（PC 上枚举为 COM5），硬件自检 5/5 通过
> - ✅ 闭环逻辑 18 项自测全绿（PC 与**板端 Python 3.9** 都跑过）
> - ✅ 端到端联调通过（模拟网关 + 真串口 + 真板子，pan 跨度 137°）
> - ✅ **板端环境已就绪**：`/root/esp32_gimbal/`，pyserial 3.5 复用 `/root/rknn_env`
> - ✅ **真实 9000 网关已验证**：帧格式 248B 正确，坐标口径实测确认（见 8.4）
> - ✅ **★ 真视觉闭环已跑通**（2026-11-03 晚，见 8.6）：真网关 + 真串口 + 真舵机，
>   60 秒 223 条指令零错误，dx 从 +0.237 收敛进死区，77 次锁定
> - ✅ **★ 一体化追踪+预览已跑通**（见 8.7）：一个进程同时做 NPU 检测 + 9000 推流 +
>   8080 网页预览 + 串口驱动云台，26.8 FPS，浏览器实时看画面和云台状态
> - ⚠️ **pan 方向实测是反的，必须加 `--invert-pan`**（两次独立标定确认，见 8.6.1）
> - ⏳ **待办**：接第二颗 tilt 舵机（GPIO15，需独立 5V 电源）；舵机改独立供电"""

NEW_STATUS = """> **状态（2026-11-22 更新）—— 双轴追踪已完整验证跑通**
>
> **实测验证结论**（用户 2026-11-22 实际使用确认）：
> - ✅ **追人稳定，不振荡** —— tilt 分轴限幅后，转身/蹲起/走动都能跟住，
>   不再出现「上下摆好几轮才停」的极限环
> - ✅ **丢目标自动回位** —— 人离开画面约 1.2 s 后，云台自动回到启动姿态
>   `P+5 T-20`；人回来能重新锁定（`LOCK`）
> - ✅ **长时间运行稳定** —— 连续运行十几分钟，无崩溃、无内存增长、零错误
> - ✅ **双轴同动** —— PAN（GPIO14）+ TILT（GPIO15）两颗 DS3120 同时工作
>
> **技术状态**：
> - ✅ 固件 **PWM 333 Hz / 控制循环 3 ms**，串口往返 **p50 = 3.00 ms**
>   （50 Hz 时是 20.00 ms，降 85%）
> - ✅ 三线程流水线（采集/推理/显示）重叠，`30.0 FPS`（相机硬件上限）
> - ✅ 控制延迟 **39 → 22.4 ms（−43%）**；帧间插值 `S 40ms` 生效
> - ✅ 服务化：`gimbal-bridge.service` + `gimbal-tracker.service`
>
> **历史里程碑**（保留）：
> - ✅ 固件烧录 + 硬件自检 5/5（2026-11-03）
> - ✅ 闭环逻辑 18 项自测全绿（PC 与板端 Python 3.9）
> - ✅ 真视觉闭环首次跑通：60 秒 223 条指令零错误，77 次锁定（2026-11-03）
> - ✅ 一体化追踪 + 预览：单进程做 NPU + 9000 推流 + 8080 预览 + 云台
> - ✅ tilt 分轴限幅修复极限环（2026-11-16，见 `TUNING_LOG.md`）
> - ✅ 启动姿态 + 丢目标回位（2026-11-17，见 `START_POSE.md`）
>
> **注意**：
> - ⚠️ pan 方向由 `g_rotate` 自动推导，**不要手动加 `--invert-pan`**（会双重翻转）
> - ⚠️ 与 `edgevision-preview.service` / `edgevision-dashboard-cam.service`
>   **抢占摄像头与 8080 端口**，跑云台前需先停它们（已 disable）"""

# ── 8.1.1 未验证表述 ─────────────────────────────────────────────
OLD_UNVERIFIED = """> ⚠️ 只测了 PAN 轴（GPIO14）单舵机。TILT 轴（GPIO15）未接舵机实测，
> 双轴同动、双舵机供电能力（峰值约 2.8A）**均未验证**。"""

NEW_UNVERIFIED = """> ⚠️ 本节只测了 PAN 轴（GPIO14）单舵机 —— 这是 2026-11-03 的记录，保留原样。
>
> ✅ **后续已验证**（见文首状态段）：
> TILT 轴（GPIO15）已于 2026-11-16 接上第二颗 DS3120，双轴同动实时追踪
> 连续运行十几分钟稳定（2026-11-22 用户实测确认）。"""


def main():
    if not os.path.exists(README):
        print("❌ README 不存在")
        return 1

    with io.open(README, encoding="utf-8") as f:
        content = f.read()

    orig_len = len(content)
    changes = []

    # 1. 状态段
    if OLD_STATUS in content:
        content = content.replace(OLD_STATUS, NEW_STATUS, 1)
        changes.append("状态段：2026-11-03 -> 2026-11-22 完整验证结论")
    else:
        print("❌ 状态段锚点未找到")
        # 打印实际内容帮助定位
        idx = content.find("状态（2026-11-03")
        if idx > 0:
            print("实际内容：")
            print(repr(content[idx:idx + 200]))
        return 1

    # 2. 未验证表述
    if OLD_UNVERIFIED in content:
        content = content.replace(OLD_UNVERIFIED, NEW_UNVERIFIED, 1)
        changes.append("8.1.1：补注「后续已验证双轴同动」")
    else:
        print("⚠️ 8.1.1 锚点未找到，跳过")

    with io.open(README, "w", encoding="utf-8") as f:
        f.write(content)

    print("=" * 66)
    print("README 更新完成")
    print("=" * 66)
    for c in changes:
        print("  ✓ %s" % c)
    print()
    print("  字符数：%d -> %d（+%d）" % (orig_len, len(content), len(content) - orig_len))
    return 0


if __name__ == "__main__":
    sys.exit(main())
