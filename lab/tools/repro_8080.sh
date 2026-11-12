#!/bin/bash
# ============================================================================
#  8080 预览 · 复现脚本（cam2 版，cam1 已坏别用）
#  用法：bash /root/repro_8080.sh
#  每一步都能单独看懂，出问题时停在哪一步一目了然
# ============================================================================
set +e
BOARD_IP=$(hostname -I | awk '{print $1}')

echo "=============================================="
echo " 8080 预览复现 · 板子 IP = ${BOARD_IP}"
echo "=============================================="
echo

# ---------- [1] 前置检查 ----------
echo "[1/6] 前置检查"
BAD=0
if [ -x /root/lab/w4_edgevision_preview_real ]; then
  echo "  ✓ 二进制存在: $(ls -lh /root/lab/w4_edgevision_preview_real | awk '{print $5}')"
else
  echo "  ✗ 二进制不存在"; BAD=1
fi
if [ -f /root/edgevision/data/models/yolov8n_dflfree_rk3588_i8.rknn ]; then
  echo "  ✓ 模型存在: $(ls -lh /root/edgevision/data/models/yolov8n_dflfree_rk3588_i8.rknn | awk '{print $5}')"
else
  echo "  ✗ 模型不存在"; BAD=1
fi
[ "$BAD" = 1 ] && { echo "前置条件不满足，退出"; exit 1; }
echo

# ---------- [2] 清场：旧的必须死干净 ----------
echo "[2/6] 清场（旧实例不清 = 摄像头被抢 = 新实例永远没帧）"
pkill -9 -f w4_edgevision_preview_real 2>/dev/null
sleep 2
LEFT=$(pgrep -cf w4_edgevision_preview_real)
echo "  残留实例数 = ${LEFT}（必须为 0）"
if [ "$LEFT" -ne 0 ]; then
  echo "  ✗ 清不掉，手工查: pgrep -af w4_edgevision_preview_real"; exit 1
fi
echo "  8080 监听数 = $(ss -ltn 2>/dev/null | grep -c ':8080')（必须为 0）"
echo "  video83 占用 = $(fuser /dev/video83 2>/dev/null || echo '空闲')"
echo

# ---------- [3] 先证明摄像头本身能出帧 ----------
echo "[3/6] 摄像头自检（这一步不过，后面全白搭）"
rm -f /root/_chk.out
timeout 10 v4l2-ctl -d /dev/video83 --stream-mmap=4 --stream-count=10 \
    --stream-to=/root/_chk.out >/dev/null 2>&1
SZ=$(stat -c%s /root/_chk.out 2>/dev/null || echo 0)
echo "  /dev/video83 抓 10 帧 = ${SZ} B"
rm -f /root/_chk.out
if [ "$SZ" -lt 1000 ]; then
  echo "  ✗ 摄像头不出帧！先查线/换口/换摄像头，别查代码"
  echo "    dmesg | grep -i uvc | tail"
  echo "    lsusb -t"
  exit 1
fi
echo "  ✓ 摄像头正常（约 $((SZ/10)) B/帧）"
echo

# ---------- [4] 后台启动 ----------
echo "[4/6] 后台启动（setsid 让它脱离 SSH，断线不死）"
cd /root/lab || exit 1
LOG=/root/lab/preview_real_cam2.log
: > "$LOG"
setsid nohup ./w4_edgevision_preview_real /dev/video83 0.25 \
    > "$LOG" 2>&1 < /dev/null &
echo "  已拉起，等 12 秒（NPU 初始化 + AE 预热 15 帧 + HTTP bind）..."
sleep 12
echo

# ---------- [5] 验证：三层判据，缺一不可 ----------
echo "[5/6] 验证"
PID=$(pgrep -f w4_edgevision_preview_real | head -1)
echo "  PID = ${PID:-无}"
if [ -z "$PID" ]; then
  echo "  ✗ 进程没了，看日志:"; tail -20 "$LOG"; exit 1
fi
echo "  线程数 = $(ls /proc/$PID/task | wc -l)（应 >= 9）"
echo "  8080 监听 = $(ss -ltn 2>/dev/null | grep -c ':8080')（必须为 1）"

# 判据一：首页有响应体
echo "  --- 判据一：首页必须有响应体 ---"
timeout 10 curl -s -o /dev/null \
  -w "  GET /         => HTTP %{http_code}, %{size_download} B, %{time_total}s\n" \
  http://127.0.0.1:8080/

# 判据二：快照是真 JPEG
echo "  --- 判据二：/snapshot 必须是真 JPEG ---"
timeout 15 curl -s -o /root/_verify.jpg \
  -w "  GET /snapshot => HTTP %{http_code}, %{size_download} B, %{time_total}s\n" \
  http://127.0.0.1:8080/snapshot
if [ -s /root/_verify.jpg ]; then
  MAGIC=$(xxd -l 2 -p /root/_verify.jpg)
  echo "  JPEG magic = ${MAGIC}（必须是 ffd8）"
  [ "$MAGIC" = "ffd8" ] && echo "  ✓ 真 JPEG" || echo "  ✗ 不是 JPEG"
  cp /root/_verify.jpg /root/lab/_verify_snapshot.jpg
else
  echo "  ✗ 快照为空"
fi
rm -f /root/_verify.jpg

# 判据三：MJPEG 流在持续出帧
echo "  --- 判据三：/stream 3 秒内要收到多帧 ---"
timeout 5 curl -s http://127.0.0.1:8080/stream -o /root/_stream.bin --max-time 3 2>/dev/null
FRAMES=$(grep -ao 'Content-Type: image/jpeg' /root/_stream.bin 2>/dev/null | wc -l)
echo "  3 秒收到 ${FRAMES} 帧（>10 为正常，约 27fps）"
rm -f /root/_stream.bin
echo

# ---------- [6] 告诉你怎么看 ----------
echo "[6/6] 打开浏览器"
echo "  ┌─────────────────────────────────────────────┐"
echo "  │  http://${BOARD_IP}:8080                    │"
echo "  └─────────────────────────────────────────────┘"
echo "  其他端点："
echo "    http://${BOARD_IP}:8080/stream     纯 MJPEG 流"
echo "    http://${BOARD_IP}:8080/snapshot   单帧 JPEG"
echo
echo "  HUD 解读：NPU 15.8ms + Dec 1.2ms | 27.7 FPS | 3D Targets: N"
echo "    3D Targets 为 0 = 画面里没有可检出的目标（不是故障）"
echo
echo "  日志： tail -f ${LOG}"
echo "  停止： pkill -f w4_edgevision_preview_real"
