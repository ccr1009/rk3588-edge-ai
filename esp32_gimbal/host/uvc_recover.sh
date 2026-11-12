#!/bin/bash
# ============================================================================
#  uvc_recover.sh —— UVC 摄像头卡死后复位（不用拔线、不用重启板子）
#
#  什么时候用
#  ----------
#  程序一直刷这个，且**一帧都读不到**：
#
#      [ WARN:0] ... cap_v4l.cpp (1004) tryIoctl VIDEOIO(V4L2:/dev/video81):
#                select() timeout.
#
#  `select() timeout` 的含义是：我们已经在等驱动出帧了，但驱动**一个字节都没给**。
#  这跟"断帧"（给了一包坏的）不是一回事 —— 断帧好歹在出数据，
#  这个是真·没数据。常见成因：
#    - 上一个进程被 `Aborted` / `kill -9` 杀掉，**没走释放路径**，
#      uvcvideo 还以为有人占着流（停止推流是靠 ioctl 通知的，进程硬死就没了）
#    - USB 带宽/供电被舵机拉垮，设备进了半死状态
#    - 另一颗摄像头或另一个进程正在抢同一个 USB 控制器
#
#  原理
#  ----
#  把 uvcvideo 驱动从该 USB 接口上**解绑**（unbind），内核会拆掉整条 video 链路；
#  再**绑回**（bind），内核重新走一遍 probe → 重新枚举 → 重建 video 节点。
#  这套做法本仓库已在 CH34x 串口上验证过（见 host/usb_reset.sh），机制相同。
#
#  ⚠️ 重要副作用：重新枚举后 **/dev/videoN 的编号可能变**
#     （原来是 video81，回来可能是 video81，也可能变成别的）。
#     脚本最后会打印复位后的实际节点，别想当然以为还是原来那个。
#
#  用法
#  ----
#      bash uvc_recover.sh              # 自动找 /dev/video81 对应的 USB 接口
#      bash uvc_recover.sh /dev/video83  # 复位另一颗
#      bash uvc_recover.sh --list        # 只看拓扑，不动手
#
#  需要 root。
# ============================================================================
set -u

TARGET="${1:-/dev/video81}"
DO_RESET=1
[ "$TARGET" = "--list" ] && { DO_RESET=0; TARGET=/dev/video81; }

echo "=============================================================="
echo " UVC 摄像头复位"
echo "=============================================================="
[ "$(id -u)" = "0" ] || { echo "[-] 需要 root"; exit 1; }

# ---- 1. 先看清楚：谁在占着设备 ----
echo
echo "[1/4] 当前占用情况"
if command -v fuser >/dev/null 2>&1; then
    # fuser 报 PID 但 ps 查不到 = 陈旧内核记录，不一定是真占用（第 15 条同类坑）
    fuser -v "$TARGET" 2>&1 | sed 's/^/      /' || true
else
    echo "      (没有 fuser，跳过)"
fi
echo "      --- 相关进程 ---"
# ★ 排除自己：pgrep -f 会匹配到本脚本的命令行（第 42 条同类坑），
#   不排除的话这里会显示一行莫名其妙的 bash，让人误以为有别的进程在跑
pgrep -af 'w4_edgevision_(gimbal_preview|gateway|preview_real)' 2>/dev/null \
    | grep -v "$$" | grep -v uvc_recover | sed 's/^/      /' || echo "      (无)"

# ---- 2. 定位 /dev/videoN 属于哪个 USB 接口 ----
echo
echo "[2/4] 定位 $TARGET 的 USB 接口"
if [ ! -e "$TARGET" ]; then
    echo "      [-] $TARGET 不存在，可能已经掉了。列出当前所有 video 节点："
    ls /dev/video* 2>/dev/null | sed 's/^/          /' || echo "          (一个都没有)"
    SYSFS=""
else
    # /sys/class/video4linux/video81/device 指向该 video 节点的 USB interface
    SYSFS=$(readlink -f "/sys/class/video4linux/$(basename "$TARGET")/device" 2>/dev/null)
    echo "      节点  : $TARGET"
    echo "      sysfs : ${SYSFS:-(解析失败)}"
fi

IFACE=""
if [ -n "${SYSFS:-}" ]; then
    IFACE=$(basename "$SYSFS")
    echo "      接口名: $IFACE"

    # 往上找 USB 设备层（接口是 X-Y:1.0，设备是 X-Y）
    USBPATH=$(echo "$SYSFS" | grep -oE '[0-9]+-[0-9]+(\.[0-9]+)*' | tail -1)
    VID=$(cat "$SYSFS/../idVendor" 2>/dev/null)
    PID=$(cat "$SYSFS/../idProduct" 2>/dev/null)
    PROD=$(cat "$SYSFS/../product" 2>/dev/null)
    echo "      USB   : $USBPATH  ${VID:-?}:${PID:-?}  ${PROD:-?}"
fi

echo
echo "      --- 全部 UVC 设备（判断有没有两个挤在一起）---"
for d in /sys/class/video4linux/video*/device; do
    [ -e "$d" ] || continue
    n=$(basename "$(dirname "$d")")
    p=$(readlink -f "$d")
    pn=$(cat "$p/../product" 2>/dev/null)
    bus=$(echo "$p" | grep -oE '[0-9]+-[0-9]+(\.[0-9]+)*' | tail -1)
    printf "      %-14s bus=%-8s %s\n" "$n" "${bus:-?}" "${pn:-?}"
done

[ "$DO_RESET" = "0" ] && { echo; echo "[i] --list 模式，到此为止"; exit 0; }

if [ -z "$IFACE" ]; then
    echo
    echo "[-] 拿不到接口名，无法自动复位。手动办法（二选一）："
    echo "    ① 拔插摄像头 USB 线（最稳）"
    echo "    ② ls /sys/bus/usb/drivers/uvcvideo/ 看有哪些接口，手工 unbind/bind"
    exit 1
fi

# ---- 3. 杀掉可能还占着设备的进程 ----
echo
echo "[3/4] 停掉占设备的进程"
for p in w4_edgevision_gimbal_preview w4_edgevision_gateway w4_edgevision_preview_real; do
    # pgrep -f 会连本脚本一起匹配，用 [首字母] 技巧避开自匹配
    if pgrep -f "[${p:0:1}]${p:1}" >/dev/null 2>&1; then
        pkill -9 -f "[${p:0:1}]${p:1}" && echo "      已杀 $p"
    fi
done
systemctl stop edgevision 2>/dev/null && echo "      edgevision 已停" || true
sleep 1

# ---- 4. 解绑 → 等待 → 重绑 ----
echo
echo "[4/4] 解绑 / 重绑 uvcvideo"
DRV=/sys/bus/usb/drivers/uvcvideo
if [ ! -d "$DRV" ]; then
    echo "      [-] 没有 $DRV，uvcvideo 可能不是模块/没加载"
    echo "          试试： modprobe -r uvcvideo && modprobe uvcvideo"
    exit 1
fi

if [ ! -e "$DRV/$IFACE" ]; then
    echo "      [!] $IFACE 目前没绑在 uvcvideo 上（列表：$(ls "$DRV" | grep -E '^[0-9]' | tr '\n' ' '))"
    echo "          直接尝试 bind"
else
    echo "$IFACE" > "$DRV/unbind" 2>&1 && echo "      unbind 成功" || echo "      [-] unbind 失败"
fi

sleep 2
echo "$IFACE" > "$DRV/bind" 2>&1 && echo "      bind   成功" || echo "      [-] bind 失败（有时是正常的：设备可能自己重新枚举了）"
sleep 3

# ---- 收尾：报告复位后的实际节点 ----
echo
echo "=== 复位后的 video 节点 ==="
ls /dev/video* 2>/dev/null | sed 's/^/      /' || echo "      (一个都没有 —— 设备没回来，拔插吧)"
echo
echo "=== 各节点的格式支持（确认能出 MJPEG）==="
for n in /dev/video*; do
    [ -e "$n" ] || continue
    if command -v v4l2-ctl >/dev/null 2>&1; then
        fmt=$(v4l2-ctl -d "$n" --list-formats 2>/dev/null | grep -oE "'[A-Z0-9]{4}'" | tr -d "'" | tr '\n' ' ')
        echo "      $n : ${fmt:-<无格式信息，可能不是采集节点>}"
    fi
done

echo
echo "=== 最近的内核/USB 报错（判断是不是硬件层）==="
dmesg 2>/dev/null | tail -40 | grep -iE 'uvc|usb|xhci|video' | tail -12 | sed 's/^/      /' || echo "      (无)"
echo
echo "--------------------------------------------------------------"
echo " 复位完成。下一步：重跑 bash /root/lab/run_gimbal_tracking.sh"
echo " ⚠️ 记住看上面的节点名，编号可能变了（原来 video81 现在不一定是）"
echo "--------------------------------------------------------------"
