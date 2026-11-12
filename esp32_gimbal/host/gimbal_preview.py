#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gimbal_preview.py — 云台追踪的实时网页预览（MJPEG over HTTP）

为什么单独做一个推流服务，而不是直接开 8080 那个：
    AGENTS.md 第⑥条记着「/dev/video81 独占：8080 预览 × 9000 网关会花屏」。
    而 9000 网关正在用 /dev/video81，所以预览必须走**另一个采集节点**。
    本板实测 /dev/video83 可以在网关占用 81 的同时独立采集（1280x720 连续 10/10 帧）。

依赖：只用 Python 标准库 + cv2（板上已有 cv2 4.9.0），不用装 flask。

用法（板端）：
    /root/rknn_env/bin/python gimbal_preview.py                    # 默认 8080 端口
    /root/rknn_env/bin/python gimbal_preview.py --port 8080 --dev /dev/video83

然后电脑浏览器打开：
    http://<board-ip>:8080/          → 画面
    http://<board-ip>:8080/status    → JSON 状态（帧率/分辨率/错误计数）

设计要点（都是踩过的坑）：
  ① 采集线程与分发解耦：多个浏览器同时看时，不能让每个连接各自去 open 摄像头。
     这里只开**一个** VideoCapture，把最新帧放进共享槽，所有客户端读同一份。
  ② 帧率上限：UVC 1280x720 MJPG 给 30fps，但网页端 MJPEG 30fps 在千兆内网
     也够呛（每帧 JPEG ~60KB → 1.8MB/s）。默认限到 20fps。
  ③ 断连要收敛：客户端关掉页面时 socket 写会抛 BrokenPipe/ConnectionReset，
     不捕获的话整个服务会挂。真实场景里"关网页"是常态。
  ④ 摄像头掉线要能自愈：USB 供电不稳时 video83 可能消失（本项目踩过 error -71），
     采集线程失败后自动重连，不要让整个服务退出。
"""

import argparse
import json
import socket
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")

try:
    import cv2
except ImportError:
    print("缺 opencv：板上已有 /root/rknn_env/bin/python -c 'import cv2'",
          file=sys.stderr)
    sys.exit(2)

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FrameGrabber:
    """单例采集：一个 VideoCapture，多个 HTTP 客户端共享最新帧。"""

    def __init__(self, dev, width, height, fps_cap=20):
        self.dev = dev
        self.width = width
        self.height = height
        self.fps_cap = fps_cap
        self.min_period = 1.0 / fps_cap if fps_cap > 0 else 0.0

        self.frame = None          # 最新 JPEG 字节
        self.lock = threading.Lock()
        self.seq = 0               # 帧序号，客户端据此判断有没有新帧
        self.frames = 0
        self.fails = 0
        self.reconnects = 0
        self.running = True
        self.started_at = time.time()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _open(self):
        cap = cv2.VideoCapture(self.dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        # 缓冲设 1，避免读到旧帧（UVC 默认会排几帧，导致画面滞后）
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _loop(self):
        cap = None
        while self.running:
            if cap is None:
                cap = self._open()
                if cap is None:
                    self.fails += 1
                    print("[preview] 打不开 %s，2s 后重试" % self.dev,
                          file=sys.stderr)
                    time.sleep(2.0)
                    continue
                print("[preview] 已打开 %s (%dx%d)"
                      % (self.dev, self.width, self.height), file=sys.stderr)

            t0 = time.time()
            ok, frame = cap.read()
            if not ok or frame is None:
                self.fails += 1
                print("[preview] 读帧失败 %d 次，重连" % self.fails, file=sys.stderr)
                try:
                    cap.release()
                except Exception:
                    pass
                cap = None
                self.reconnects += 1
                time.sleep(1.0)
                continue

            # 缩放到设定分辨率（摄像头可能给的不是请求的尺寸）
            h, w = frame.shape[:2]
            if (w, h) != (self.width, self.height):
                frame = cv2.resize(frame, (self.width, self.height),
                                   interpolation=cv2.INTER_AREA)

            ok2, jpg = cv2.imencode(".jpg", frame,
                                    [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            if ok2:
                with self.lock:
                    self.frame = jpg.tobytes()
                    self.seq += 1
                    self.frames += 1

            # 限速
            dt = time.time() - t0
            if dt < self.min_period:
                time.sleep(self.min_period - dt)

        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def snapshot(self):
        with self.lock:
            return self.frame, self.seq

    def stats(self):
        with self.lock:
            el = max(time.time() - self.started_at, 1e-6)
            return {
                "dev": self.dev,
                "width": self.width,
                "height": self.height,
                "fps_cap": self.fps_cap,
                "frames": self.frames,
                "fails": self.fails,
                "reconnects": self.reconnects,
                "avg_fps": round(self.frames / el, 2),
                "uptime_s": round(el, 1),
            }


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>云台追踪预览</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#0b0d10; color:#e6e6e6;
         font-family: ui-monospace, Consolas, monospace; }
  header { padding:10px 14px; background:#151920;
           border-bottom:1px solid #262c36; display:flex;
           align-items:baseline; gap:14px; flex-wrap:wrap; }
  h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.5px; }
  .stat { font-size:12px; color:#8b95a5; }
  .stat b { color:#4ade80; font-weight:600; }
  main { position:relative; display:flex; justify-content:center;
         align-items:center; min-height:calc(100vh - 44px); padding:10px; }
  #wrap { position:relative; line-height:0; }
  img { max-width:100%; max-height:calc(100vh - 70px);
        border:1px solid #262c36; border-radius:6px; background:#000; }
  /* 画面中心十字，方便判断目标是否居中 */
  #cross { position:absolute; inset:0; pointer-events:none; }
  #cross::before, #cross::after {
    content:""; position:absolute; background:rgba(74,222,128,.55); }
  #cross::before { left:50%; top:0; bottom:0; width:1px; }
  #cross::after  { top:50%; left:0; right:0; height:1px; }
  #err { position:fixed; left:14px; bottom:14px; font-size:12px;
         color:#f87171; background:#1a1113; padding:6px 10px;
         border-radius:4px; display:none; max-width:70vw; }
</style>
</head>
<body>
<header>
  <h1>云台追踪 · 实时预览</h1>
  <span class="stat">设备 <b id="dev">-</b></span>
  <span class="stat">分辨率 <b id="res">-</b></span>
  <span class="stat">实测 <b id="fps">-</b> fps</span>
  <span class="stat">帧数 <b id="cnt">-</b></span>
  <span class="stat">重连 <b id="rec">-</b></span>
</header>
<main>
  <div id="wrap">
    <img id="v" src="/stream" alt="预览流">
    <div id="cross"></div>
  </div>
</main>
<div id="err"></div>
<script>
// 状态轮询：每 1s 拉一次 /status，把服务端统计显示出来
async function tick() {
  try {
    const r = await fetch('/status', {cache:'no-store'});
    const s = await r.json();
    document.getElementById('dev').textContent = s.dev;
    document.getElementById('res').textContent = s.width + 'x' + s.height;
    document.getElementById('fps').textContent = s.avg_fps;
    document.getElementById('cnt').textContent = s.frames;
    document.getElementById('rec').textContent = s.reconnects;
    document.getElementById('err').style.display = 'none';
  } catch (e) {
    const el = document.getElementById('err');
    el.textContent = '状态获取失败：' + e;
    el.style.display = 'block';
  }
}
setInterval(tick, 1000); tick();

// 图片流断了自动重连（摄像头重连时 /stream 会结束）
const img = document.getElementById('v');
img.addEventListener('error', () => {
  setTimeout(() => {
    img.src = '/stream?t=' + Date.now();
  }, 800);
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    grabber = None            # 由 main() 注入

    # 默认的 log_message 会把每个请求打到 stderr，MJPEG 会刷屏
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            return

        if path == "/status":
            body = json.dumps(self.grabber.stats(), ensure_ascii=False).encode()
            self._send(200, "application/json; charset=utf-8", body)
            return

        if path == "/stream":
            self._stream()
            return

        self._send(404, "text/plain; charset=utf-8", b"404")

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        last_seq = -1
        try:
            while True:
                frame, seq = self.grabber.snapshot()
                if frame is None or seq == last_seq:
                    time.sleep(0.005)
                    continue
                last_seq = seq
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(("Content-Length: %d\r\n\r\n"
                                  % len(frame)).encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 客户端关页面 —— 正常现象，不是错误
            pass


def main():
    ap = argparse.ArgumentParser(description="云台追踪实时网页预览（MJPEG）")
    ap.add_argument("--dev", default="/dev/video83",
                    help="采集设备。默认 video83（避开 9000 网关独占的 video81）")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=float, default=20.0,
                    help="推送帧率上限（默认 20，1280x720 JPEG ~60KB/帧 ≈ 1.2MB/s）")
    args = ap.parse_args()

    grab = FrameGrabber(args.dev, args.width, args.height, fps_cap=args.fps)
    grab.start()
    time.sleep(0.5)     # 等第一帧，避免首页打开时空白

    Handler.grabber = grab
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.daemon_threads = True

    print("=" * 64)
    print("云台追踪预览已启动")
    print("  设备   %s  (%dx%d, 限 %.0f fps)"
          % (args.dev, args.width, args.height, args.fps))
    print("  监听   http://%s:%d/" % (args.bind, args.port))
    print("  画面   http://<板子IP>:%d/" % args.port)
    print("  状态   http://<板子IP>:%d/status" % args.port)
    print("  Ctrl-C 结束")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[退出]")
    finally:
        grab.running = False
        httpd.server_close()


if __name__ == "__main__":
    main()
