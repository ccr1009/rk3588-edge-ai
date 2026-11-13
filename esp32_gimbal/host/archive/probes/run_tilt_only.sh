#!/bin/bash
# run_tilt_only.sh — 只有一颗好舵机（带动俯仰）时，跑单轴追踪
#
# ═══════════════════════════════════════════════════════════════════
# 当前硬件错位（2026-11-06 实测）
# ═══════════════════════════════════════════════════════════════════
#
#   物理上：唯一的好舵机带动【顶部俯仰】（抬头/低头）
#   电 气 上：它插在 GPIO14
#   固 件 里：GPIO14 默认是 PAN 通道（水平）
#   同时：底部 pan 舵机已烧毁，接在 GPIO13，不工作
#
#   如果直接启动，程序会拿「目标横向偏移 dx」去驱动俯仰 —— 行为完全错乱。
#
# ── 解决办法：用固件的 G/T 命令做运行时重映射（不用改代码、不用挪线）──
#
#     G 15    把 PAN 通道切到 GPIO15（空脚，不会误驱动任何东西）
#     T 14    把 TILT 通道切到 GPIO14（= 你的俯仰舵机，语义正确了）
#
#   重映射后：
#     PAN  → GPIO15（空）
#     TILT → GPIO14（俯仰舵机）✅
#
#   再用 --no-pan 让程序不下发 pan 指令，只控制 tilt。
#
#   ⚠️ 重映射只在运行时有效，ESP32 重启/复位后回到默认 14/15。
#      所以每次启动前都要重新发 G/T —— 本脚本会做。
# ═══════════════════════════════════════════════════════════════════

set -u

echo "================================================================"
echo " 单轴（俯仰）追踪 —— 自动重映射引脚"
echo "================================================================"
echo

# ---------- 1. 检查固件和引脚 ----------
echo "--- 1. 检查 ESP32 链路 ---"
if ! /root/rknn_env/bin/python /root/esp32_gimbal/check_link.py 2>&1 | grep -q 'GPIN'; then
    echo "  ❌ ESP32 通信失败"
    echo "     先跑： bash /root/esp32_gimbal/stop_bridge.sh"
    echo "     再跑： /root/rknn_env/bin/python /root/esp32_gimbal/check_link.py"
    exit 1
fi
echo "  ✅ 通信正常"

# ---------- 2. 停掉旧进程，释放 USB ----------
echo
echo "--- 2. 停旧进程 ---"
for pid in $(pgrep -f 'w4_edgevision_gimbal_preview' 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
    echo "  杀掉 preview PID=$pid"
done
for pid in $(pgrep -f 'gimbal_bridge.py' 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
    echo "  杀掉 bridge PID=$pid"
done
systemctl stop edgevision 2>/dev/null
sleep 2

# ---------- 3. 重映射引脚（关键步骤）----------
echo
echo "--- 3. 重映射引脚（PAN→15 空脚，TILT→14 俯仰舵机）---"
cat > /tmp/_remap.py <<'PYEOF'
import sys, time
sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial

s = UsbCdcSerial(timeout=0.5)
s.reset_input_buffer()

def cmd(c, w=0.9):
    s.write((c + "\n").encode()); s.flush()
    t0 = time.time()
    while time.time() - t0 < w:
        l = s.readline()
        if l:
            v = l.decode("utf-8", "replace").strip()
            if v: return v
    return None

# 清场：只发停止类命令，绝不发 X（X=引脚扫描，会让 P 失效）
for c in ["D 0", "A"]:
    cmd(c, 0.4)
time.sleep(0.4)
s.reset_input_buffer()

before = cmd("I", 1.0)
print("  重映射前: %s" % before)

cmd("G 15", 0.9)      # PAN 通道 → GPIO15（空脚）
r1 = cmd("I", 0.9)
cmd("T 14", 0.9)      # TILT 通道 → GPIO14（俯仰舵机）
r2 = cmd("I", 0.9)

print("  G 15 后 : %s" % r1)
print("  T 14 后 : %s" % r2)

# 中位
cmd("P 0 0", 0.5)
s.close()

if r2 and "GPIN 15 14" in r2:
    print("  ✅ 映射正确：PAN=GPIO15(空)  TILT=GPIO14(俯仰舵机)")
    sys.exit(0)
else:
    print("  ⚠️ 映射结果异常: %s" % r2)
    sys.exit(1)
PYEOF

if ! /root/rknn_env/bin/python /tmp/_remap.py; then
    echo "  重映射失败，退出"
    exit 1
fi

# ---------- 4. 起桥接 ----------
echo
echo "--- 4. 启动桥接服务 ---"
cd /root/esp32_gimbal || exit 1
nohup /root/rknn_env/bin/python gimbal_bridge.py --port 9100 > /tmp/bridge.log 2>&1 &
sleep 5
if ss -tln 2>/dev/null | grep -q 9100; then
    echo "  ✅ 桥接已监听 9100"
else
    echo "  ❌ 桥接没起来，日志："
    cat /tmp/bridge.log
    exit 1
fi

# ---------- 5. 起追踪 ----------
echo
echo "--- 5. 启动追踪（只开 tilt 轴）---"
cd /root/lab || exit 1
nohup ./w4_edgevision_gimbal_preview /dev/video81 \
    --bridge 127.0.0.1:9100 \
    --no-pan \
    --no-3d \
    > /tmp/tilt_track.log 2>&1 &
PID=$!
echo "  preview PID=$PID"
sleep 8

if ! kill -0 "$PID" 2>/dev/null; then
    echo "  ❌ 程序已退出，日志："
    tail -30 /tmp/tilt_track.log
    exit 1
fi

echo
echo "--- 6. 日志 ---"
tail -15 /tmp/tilt_track.log

BOARD_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "================================================================"
echo " 追踪已启动"
echo "================================================================"
echo " 浏览器打开： http://${BOARD_IP:-<板子IP>}:8080/"
echo
echo " 判据（你现在只有俯仰轴能动）："
echo "   · 靠近/远离摄像头 → 云台抬头 / 低头跟随"
echo "   · 左右走动         → 云台不应该水平转（pan 已关）"
echo
echo " 如果抬头方向反了："
echo "   bash /root/esp32_gimbal/stop_tilt_only.sh"
echo "   然后手动加 --no-invert-tilt 重跑"
echo
echo " 停止： bash /root/esp32_gimbal/stop_tilt_only.sh"
echo "================================================================"
