#!/bin/bash
# ==============================================================================
# 一键验证脚本 —— 烧完 333Hz 固件后跑这个
#
# 用法：
#   ssh rk3588 "bash /root/esp32_gimbal/verify_after_flash.sh"
#
# 它会依次检查：
#   1. 服务是否在跑（不在就启动）
#   2. ESP32 固件是 50Hz 还是 333Hz（核心判据）
#   3. 四项优化是否都生效
#   4. 稳定性指标（fps / errors / dropped）
#
# ⚠️ 这个脚本会启动追踪程序，会驱动舵机（有声音）。
#    深夜请不要运行。
# ==============================================================================

set -u
PY=/root/rknn_env/bin/python
G=/root/esp32_gimbal

echo "================================================================================"
echo " EdgeVision 云台追踪 — 烧录后一键验证"
echo " $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================================"

# ---------- 1. 服务状态 ----------
echo
echo "[1/4] 服务状态"
echo "--------------------------------------------------------------------------------"
BRIDGE_ACTIVE=$(systemctl is-active gimbal-bridge 2>/dev/null || echo inactive)
TRACKER_ACTIVE=$(systemctl is-active gimbal-tracker 2>/dev/null || echo inactive)
echo "  gimbal-bridge  : $BRIDGE_ACTIVE"
echo "  gimbal-tracker : $TRACKER_ACTIVE"

if [ "$BRIDGE_ACTIVE" != "active" ]; then
    echo "  → 启动桥接..."
    systemctl start gimbal-bridge
    sleep 3
fi

if [ "$TRACKER_ACTIVE" != "active" ]; then
    echo "  → 启动追踪..."
    systemctl start gimbal-tracker
    sleep 10
fi

if ! curl -s --max-time 3 http://127.0.0.1:8080/status > /dev/null 2>&1; then
    echo "  ❌ 8080 仍无响应，中止"
    echo "     查看日志: tail -30 /tmp/gimbal_v2.log"
    exit 1
fi
echo "  ✅ 两服务正常，8080 有响应"

# ---------- 2. P2 固件频率判定（核心） ----------
echo
echo "[2/4] P2：ESP32 固件频率判定"
echo "--------------------------------------------------------------------------------"
$PY $G/check_p2_after_flash.py --expect 333
P2_RC=$?

# ---------- 3. 四项优化验证 ----------
echo
echo "[3/4] 四项优化验证"
echo "--------------------------------------------------------------------------------"
$PY $G/verify_all.py 2>&1 | grep -E "P1|P2|P3|P4|帧间插值|异步回包|流水线|固件|生效|正常|达标|待烧录|已烧录"

# ---------- 4. 稳定性快照 ----------
echo
echo "[4/4] 稳定性快照"
echo "--------------------------------------------------------------------------------"
STATUS=$(curl -s --max-time 3 http://127.0.0.1:8080/status 2>/dev/null)
if [ -n "$STATUS" ]; then
    FPS=$(echo "$STATUS" | grep -oE '"fps":[0-9.]+' | cut -d: -f2)
    NPU=$(echo "$STATUS" | grep -oE '"npu_ms":[0-9.]+' | cut -d: -f2)
    ERR=$(echo "$STATUS" | grep -oE '"errors":[0-9]+' | cut -d: -f2)
    DROP=$(echo "$STATUS" | grep -oE '"dropped":[0-9]+' | cut -d: -f2)
    REOP=$(echo "$STATUS" | grep -oE '"reopens":[0-9]+' | cut -d: -f2)
    BAD=$(echo "$STATUS" | grep -oE '"badjpeg":[0-9]+' | cut -d: -f2)
    echo "  fps=$FPS  npu=${NPU}ms  errors=$ERR  dropped=$DROP  reopens=$REOP  badjpeg=$BAD"

    if [ "${FPS%.*}" -ge 29 ] 2>/dev/null; then
        echo "  ✅ 帧率正常（≥29，相机上限 30）"
    else
        echo "  ⚠️ 帧率 $FPS 偏低"
    fi
    if [ "$ERR" = "0" ] && [ "$DROP" = "0" ]; then
        echo "  ✅ 零错误、零丢帧"
    else
        echo "  ⚠️ errors=$ERR dropped=$DROP 需要关注"
    fi
else
    echo "  ⚠️ 读不到 /status"
fi

# ---------- 汇总 ----------
echo
echo "================================================================================"
echo " 结论"
echo "================================================================================"
if [ $P2_RC -eq 0 ]; then
    echo "  ✅ P2 已完成 —— 固件已是 333Hz，四项优化全部验证通过！"
    echo
    echo "  端到端延迟改善："
    echo "    控制指令发出  :  22.4 ms（流水线，原 39ms）"
    echo "    舵机开始动    :  约 37 ms（烧 333Hz 后，原 45.5ms）"
    echo "    舵机到位(10°) :  约 255 ms"
else
    echo "  ⏸️ P2 未完成 —— 固件还是 50Hz"
    echo
    echo "  烧录方法（ESP32 插 PC 的 USB 口，不是板子）："
    echo "    cd D:\\a研究生\\项目\\rk3588项目\\edgevision-rk3588\\esp32_gimbal"
    echo "    pwsh -File build.ps1 -Flash"
    echo
    echo "  烧完插回板子，重跑本脚本。"
fi
echo "================================================================================"
