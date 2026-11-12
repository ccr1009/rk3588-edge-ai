#!/usr/bin/env python3
# 严格重做方向标定：云台固定三个角度，每个角度采 15 帧，看 cx 怎么变
#
# 判据（物理直觉）：
#   摄像头装在云台上。云台往右转 → 镜头朝右 → 原本在右侧的目标
#   相对画面会**向中间/左侧**移动（因为镜头追过去了）。
#   所以：pan 增大 → cx 减小  = 负反馈，正确，不需要 invert
#         pan 增大 → cx 增大  = 正反馈，需要 invert
#
# 关键：每次换角度后必须等舵机真的到位（缓动），并丢弃过渡帧。
import sys
import socket
import time
import statistics

sys.path.insert(0, "/root/esp32_gimbal")
import serial
from gimbal_control import FRAME_SIZE, parse_frame, COCO80

PORT = "/dev/ttyUSB0"
IMG_W, IMG_H = 1280, 720
PERSON = COCO80.index("person")

ser = serial.Serial(PORT, 115200, timeout=1.0)
time.sleep(0.3)
ser.reset_input_buffer()


def send(cmd, wait=1.0):
    ser.write((cmd + "\n").encode())
    ser.flush()
    time.sleep(wait)
    return ser.read(300).decode(errors="replace").strip()


def grab_once():
    try:
        s = socket.create_connection(("127.0.0.1", 9000), timeout=1.0)
    except OSError:
        return None
    buf = bytearray()
    s.settimeout(1.0)
    try:
        while len(buf) < FRAME_SIZE:
            c = s.recv(FRAME_SIZE - len(buf))
            if not c:
                break
            buf.extend(c)
    except Exception:
        pass
    finally:
        s.close()
    if len(buf) != FRAME_SIZE:
        return None
    return parse_frame(bytes(buf), IMG_W, IMG_H)


def sample(n=15):
    """采 n 个**新**帧（按 frame_id 去重），返回 person 框 cx 列表"""
    xs = []
    last = None
    t0 = time.time()
    while len(xs) < n and time.time() - t0 < 8:
        r = grab_once()
        if r is None:
            continue
        fid, boxes = r
        if fid == last:
            time.sleep(0.01)
            continue
        last = fid
        cand = [b for b in boxes if b["class_id"] == PERSON]
        if not cand:
            continue
        best = max(cand, key=lambda b: b["w"] * b["h"])
        xs.append(best["cx"] * IMG_W)
    return xs


print("=" * 68)
print("方向标定：三位置 × 15 帧")
print("=" * 68)
print()

# 先回中
print("回中 ...", send("H", 1.2))
print()

results = {}
for pan in (-20, 0, 20):
    print("--- pan = %+d ---" % pan)
    print("  下发:", send("P %d 0" % pan, 1.0))
    # 等缓动到位（固件缓动 + 机械惯性），丢弃前 0.6s 的过渡帧
    time.sleep(0.6)
    xs = sample(15)
    results[pan] = xs
    if xs:
        print("  cx: n=%d 均值=%.1f 中位=%.1f 范围[%.1f, %.1f]"
              % (len(xs), statistics.mean(xs), statistics.median(xs),
                 min(xs), max(xs)))
    else:
        print("  (没采到 person)")
    print()

# 分析
valid = {p: xs for p, xs in results.items() if len(xs) >= 8}
print("=" * 68)
if len(valid) < 2:
    print("样本不足，无法判定。请确保人站在画面里。")
else:
    pan_min = min(valid)
    pan_max = max(valid)
    cx_min_pan = statistics.median(valid[pan_min])
    cx_max_pan = statistics.median(valid[pan_max])
    delta = cx_max_pan - cx_min_pan

    print("Δcx = cx(pan=%+d) - cx(pan=%+d) = %.1f - %.1f = %+.1f px"
          % (pan_max, pan_min, cx_max_pan, cx_min_pan, delta))
    print()

    # 阈值按框大小自适应：框越大，同样的转角 cx 变化越大
    all_box = []
    for xs in valid.values():
        all_box.extend(xs)
    spread = statistics.median(all_box)
    threshold = 25.0

    if delta < -threshold:
        print("判定：pan↑ → cx↓（目标在画面里左移）")
        print("      = 负反馈，方向【正确】✅  不需要 --invert-pan")
        print("      => 代码里应该把 invert_pan 设为 false")
    elif delta > threshold:
        print("判定：pan↑ → cx↑（目标在画面里右移）")
        print("      = 正反馈，方向【反了】❌  需要 --invert-pan")
    else:
        print("判定：Δcx=%.1f px 太小，无法判定" % delta)
        print("      可能：目标不在画面里 / 舵机没转 / 人也在动")
    print()
    print("补充：cx 中位在 pan=%+d 时是 %.1f，画面中心是 %.0f"
          % (0 if 0 in valid else pan_min,
             statistics.median(valid.get(0, valid[pan_min])), IMG_W / 2))
print("=" * 68)

send("H", 1.0)
ser.close()
