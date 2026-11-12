#!/usr/bin/env bash
# 启动追踪（前台跑，Ctrl-C 结束）
#
# 用法：
#   bash /root/esp32_gimbal/track.sh              # 默认跑 60 秒
#   bash /root/esp32_gimbal/track.sh 120          # 跑 120 秒
#   bash /root/esp32_gimbal/track.sh 0            # 一直跑到 Ctrl-C
#
# 参数为什么这么定（run_tracking.ps1 里的实测标定，别随便改）：
#   --invert-pan  --invert-tilt  ★ 两个都要。2026-11-03 实测本套硬件 pan 反、
#                                  tilt 也反；只加 pan 时 tilt 仍顶在 -45°
#   --kp-pan 0.20 --kp-tilt 0.15  默认 0.45/0.40 太大，会立刻振荡
#   --max-step 2.5                默认 6.0 = 102°/秒，1 秒甩到极限
cd /root/esp32_gimbal || exit 1

SECS="${1:-60}"
PY=/root/rknn_env/bin/python
PORT="${GIMBAL_PORT:-/dev/ttyUSB0}"

# ---- 前置检查（缺一个就直接告诉你，不让你对着空画面等）----
fail=0
if [ ! -e "$PORT" ]; then
    echo "[X] 串口 $PORT 不存在" >&2
    echo "    查：ls -l /dev/ttyUSB*; dmesg | grep -E 'ch341|error -71'" >&2
    fail=1
fi
if ! systemctl is-active --quiet edgevision; then
    echo "[!] edgevision 没在跑，正在启动 ..." >&2
    systemctl start edgevision
    sleep 8
    if ! systemctl is-active --quiet edgevision; then
        echo "[X] 网关起不来，追踪会一直拿不到帧" >&2
        fail=1
    fi
fi
if ! ss -lnt 2>/dev/null | grep -q ':9000'; then
    echo "[!] 9000 没在监听，等 5 秒再试 ..." >&2
    sleep 5
    if ! ss -lnt 2>/dev/null | grep -q ':9000'; then
        echo "[X] 9000 端口没有监听" >&2
        fail=1
    fi
fi
if [ "$fail" = "1" ]; then
    echo "" >&2
    echo "先跑 gimbal_probe.py --port $PORT 排查链路" >&2
    exit 1
fi

echo "=============================================================="
echo " 视觉追踪启动  串口=$PORT  时长=${SECS}s（0=一直跑）"
echo " 参数：kp=(0.20,0.15)  max_step=2.5  min_score=0.4  invert-pan+tilt"
echo " 画面里要有人（person，置信度 ≥0.4）"
echo "=============================================================="
echo ""

args=(
    -u "$PY" gimbal_tracker.py
    --port "$PORT"
    --class person
    --img-w 1280 --img-h 720
    --kp-pan 0.20 --kp-tilt 0.15
    --max-step 2.5
    --min-score 0.4
    --invert-pan
    --invert-tilt
)

if [ "$SECS" = "0" ]; then
    exec "${args[@]}"
else
    timeout "$SECS" "${args[@]}"
fi
