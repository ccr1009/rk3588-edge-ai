#!/bin/bash
# ============================================================================
#  EdgeVision 演示脚本（cam1 已坏，网关已切 cam2 的版本）
#
#  演示逻辑：
#    systemctl start edgevision  →  9000 网关跑起来（推 248B 定长帧）
#    三个"预览"入口各有分工，见下方表格
#
#  前提：本脚本假定 /etc/systemd/system/edgevision.service.d/cam2.conf 已存在
#        （把网关从坏的 /dev/video81 切到 /dev/video83）
# ============================================================================
set +e
BOARD_IP=$(hostname -I | awk '{print $1}')

echo "══════════════════════════════════════════════════════════"
echo "  EdgeVision 演示 · 板子 ${BOARD_IP}"
echo "══════════════════════════════════════════════════════════"
echo

echo "【1/5】检查 drop-in 是否已切到 cam2"
if [ -f /etc/systemd/system/edgevision.service.d/cam2.conf ]; then
  echo "  ✓ cam2.conf 存在"
  grep ExecStart /etc/systemd/system/edgevision.service.d/cam2.conf | sed 's/^/    /'
else
  echo "  ✗ 缺少 cam2.conf —— 网关会用坏的 cam1，演示必然失败"
  echo "    修复见命令：cat > /etc/systemd/system/edgevision.service.d/cam2.conf"
  exit 1
fi
echo

echo "【2/5】启动 9000 网关（演示主角）"
systemctl start edgevision
sleep 12
echo "  状态 = $(systemctl is-active edgevision)"
echo "  PID  = $(systemctl show -p MainPID --value edgevision)"
echo "  命令 = $(ps -o args= -p $(systemctl show -p MainPID --value edgevision) 2>/dev/null)"
echo

echo "【3/5】验证 9000 真在出帧（不是只看 active）"
timeout 20 python3 -c "
import socket,struct,time
try:
    s=socket.create_connection(('127.0.0.1',9000),timeout=10)
    n=0; t0=time.time(); first=None; last=None
    while n<20 and time.time()-t0<15:
        d=b''
        while len(d)<248:
            c=s.recv(248-len(d))
            if not c: break
            d+=c
        if len(d)<248: break
        fid,cnt=struct.unpack_from('<II',d,0)
        if first is None: first=fid
        last=fid; n+=1
    if n>0:
        print('  ✓ %d 帧 / %.2fs = %.1f FPS | frame_id %d→%d'%(n,time.time()-t0,n/(time.time()-t0),first,last))
    else:
        print('  ✗ 0 帧 —— 网关没在出帧')
    s.close()
except Exception as e:
    print('  ✗ 收帧失败: %r'%e)
" 2>&1
echo

echo "【4/5】启动 8090 看板（浏览器画面，不抢摄像头）"
if pgrep -f edgevision_dashboard >/dev/null; then
  echo "  已在运行"
else
  cd /root/edgevision
  setsid nohup ./deploy/cpp/build_release/edgevision_dashboard --port 8090 \
      > /root/lab/_dash.log 2>&1 < /dev/null &
  sleep 10
  echo "  已启动 PID=$(pgrep -f edgevision_dashboard | head -1)"
fi
timeout 10 curl -s -o /dev/null -w "  页面 => HTTP %{http_code}, %{size_download} B\n" http://127.0.0.1:8090/
echo

echo "【5/5】演示入口汇总"
echo "  ┌────────────────────────────────────────────────────────────┐"
echo "  │  ① 浏览器 3D 看板（推荐演示用）                            │"
echo "  │     http://${BOARD_IP}:8090                                │"
echo "  │     mp4 真实路景 + 每车彩色 3D 框 + BEV 雷达 + 距离标注     │"
echo "  │     可与 9000 同时跑，互不干扰                              │"
echo "  ├────────────────────────────────────────────────────────────┤"
echo "  │  ② PC 终端 9000 推流                                        │"
echo "  │     set EDGEVISION_BOARD_HOST=${BOARD_IP}                   │"
echo "  │     python lab/tests/pc_client_viewer.py                    │"
echo "  │     逐帧打印 帧号/FPS/检出目标                              │"
echo "  ├────────────────────────────────────────────────────────────┤"
echo "  │  ③ 8080 摄像头实时预览（需先停网关！）                      │"
echo "  │     systemctl stop edgevision                               │"
echo "  │     pkill -9 -f w4_edgevision_preview_real                  │"
echo "  │     cd /root/lab && ./w4_edgevision_preview_real /dev/video83 0.25 │"
echo "  │     http://${BOARD_IP}:8080                                │"
echo "  │     注意：8080 与 9000 抢同一个摄像头，不能同时开           │"
echo "  └────────────────────────────────────────────────────────────┘"
echo
echo "  当前并存状态："
echo "    edgevision(9000) = $(systemctl is-active edgevision)"
echo "    dashboard(8090)  = $([ -n "$(pgrep -f edgevision_dashboard)" ] && echo running || echo stopped)"
echo "    preview(8080)    = $(pgrep -cf w4_edgevision_preview_real)"
