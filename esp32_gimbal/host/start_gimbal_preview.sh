#!/bin/bash
# 启动一体化云台追踪预览 v2（含 9000 检测流 + 可选 tilt）
#
# 用法：
#   bash start_gimbal_preview.sh                          # 默认：追踪+tilt
#   bash start_gimbal_preview.sh /dev/ttyUSB0 --no-tilt    # 只追 pan
#   bash start_gimbal_preview.sh "" --no-tilt              # 纯预览（不驱动云台）
#
# 环境变量：
#   GIMBAL_PORT  串口，默认 /dev/ttyUSB0
#   CAM_DEV      摄像头，默认 /dev/video81
set -u

GIMBAL="${1-/dev/ttyUSB0}"
shift 2>/dev/null || true
EXTRA="$*"

CAM="${CAM_DEV:-/dev/video81}"
PORT="${GIMBAL:-${GIMBAL_PORT:-/dev/ttyUSB0}}"

echo "===== 1. 停网关释放摄像头 ====="
systemctl stop edgevision 2>/dev/null
sleep 1
if pgrep -f w4_edgevision_gateway >/dev/null 2>&1; then
    pkill -9 -f w4_edgevision_gateway
    sleep 1
fi
pgrep -af w4_edgevision_gateway >/dev/null 2>&1 && echo "  [!] 网关还在" || echo "  网关已停 ✓"

echo
echo "===== 2. 检查串口 ====="
if [ -e "$PORT" ]; then
    ls -l "$PORT"
else
    echo "  [!] $PORT 不存在，将以纯预览模式启动"
    PORT=""
fi

echo
echo "===== 3. 启动 ====="
cd /root/lab || exit 1

if [ -n "$PORT" ]; then
    exec ./w4_edgevision_gimbal_preview "$CAM" --gimbal "$PORT" $EXTRA
else
    exec ./w4_edgevision_gimbal_preview "$CAM" $EXTRA
fi
