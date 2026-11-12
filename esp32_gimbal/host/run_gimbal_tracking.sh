#!/bin/bash
# 一键运行：一体化云台追踪 + 网页预览
#
# 用法：
#   bash run_gimbal_tracking.sh                 # 追踪 pan（tilt 自动禁用，因为没接舵机）
#   bash run_gimbal_tracking.sh --tilt           # 双轴（接上第二颗舵机后用）
#   bash run_gimbal_tracking.sh ""               # 纯预览（不驱动云台，给 edgevision 替代用）
#   bash run_gimbal_tracking.sh --smooth 0.5     # 调滤波系数
#
# 这个脚本会：
#   1. 停掉 edgevision 网关（释放 /dev/video81，UVC 不支持双开）
#   2. 启动一体化程序，同时提供：
#        - 9000 检测流（248B 定长帧，与原网关协议完全一致，Agent 可直连）
#        - 8080 网页预览（MJPEG + 云台状态面板）
#        - 串口驱动 ESP32 云台（可选）
#
# 为什么必须停网关：/dev/video81 是 UVC 设备，只允许一个进程打开。
# 这是本项目 AGENTS.md 第⑥条记录的资源互斥问题。
set -u

GIMBAL="${GIMBAL_PORT:-/dev/ttyUSB0}"
CAM="${CAM_DEV:-/dev/video81}"
EXTRA="$*"

echo "=============================================================="
echo " 一体化云台追踪 + 网页预览"
echo "=============================================================="
echo

# ---- 1. 释放摄像头 ----
echo "[1/3] 停 edgevision 网关（释放 $CAM）"
systemctl stop edgevision 2>/dev/null
sleep 1
if pgrep -f w4_edgevision_gateway >/dev/null 2>&1; then
    pkill -9 -f w4_edgevision_gateway
    sleep 1
fi
echo "      网关已停 ✓"

# ★ 还要杀掉【上一次的自己】。
#
# 2026-11-11 踩到：脚本原来只杀网关，不管残留的预览进程。
# 如果上一次 w4_edgevision_gimbal_preview 没退干净（Ctrl-Z 挂起 / 被 Aborted 后
# 父进程仍在 / 在别的 tty 里还开着），它就一直占着 $CAM。
# UVC 是单进程独占，新实例的 open() 有时**照样成功**，但 DQBUF 永远拿不到帧 ——
# 表现就是满屏 `select() timeout`，看着像"摄像头坏了"，其实是被另一个进程吃掉了帧。
# 判据：`fuser -v $CAM` 里出现两个以上 PID。
if pgrep -f 'w4_edgevision_gimbal_preview' >/dev/null 2>&1; then
    echo "      [!] 发现残留的预览进程，先杀掉："
    pgrep -af 'w4_edgevision_gimbal_preview' | sed 's/^/          /'
    pkill -9 -f 'w4_edgevision_gimbal_preview'
    sleep 1
fi

# 再确认设备真的没人占（fuser 报 PID 但 ps 查不到 = 陈旧内核记录，不算占用）
if command -v fuser >/dev/null 2>&1; then
    USERS=$(fuser "$CAM" 2>/dev/null)
    if [ -n "$USERS" ]; then
        LIVE=""
        for pid in $USERS; do
            kill -0 "$pid" 2>/dev/null && LIVE="$LIVE $pid"
        done
        if [ -n "$LIVE" ]; then
            echo "      [!] $CAM 仍被占用:$LIVE"
            echo "          如果不是你认识的进程，试试 bash /root/esp32_gimbal/uvc_recover.sh"
        else
            echo "      $CAM 无存活占用者 ✓（fuser 报的是陈旧记录）"
        fi
    fi
fi

# ---- 2. 检查串口 ----
echo
echo "[2/3] 检查云台串口"
if [ -n "$GIMBAL" ] && [ -e "$GIMBAL" ]; then
    echo "      $GIMBAL 存在 ✓"
    GIMBAL_ARG="--gimbal $GIMBAL"
else
    echo "      [!] 串口不存在，以纯预览模式启动"
    GIMBAL_ARG=""
fi

# ---- 3. 启动 ----
echo
echo "[3/3] 启动"
echo "      网页: http://<板子IP>:8080/"
echo "      检测流: tcp://<板子IP>:9000"
echo
cd /root/lab || exit 1

if [ -n "$GIMBAL_ARG" ]; then
    # 默认禁用 tilt：第二颗舵机还没接，开着会一路顶到 ±45° 浪费指令
    case "$EXTRA" in
        *--tilt*) ;;   # 用户显式要双轴
        *) EXTRA="$EXTRA --no-tilt" ;;
    esac
    exec ./w4_edgevision_gimbal_preview "$CAM" $GIMBAL_ARG $EXTRA
else
    exec ./w4_edgevision_gimbal_preview "$CAM" $EXTRA
fi
