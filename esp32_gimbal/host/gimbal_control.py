#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gimbal_control.py — 云台控制律与协议解析（纯函数，无 I/O）

单独抽出来是为了让 test_loopback.py 能**直接 import 真实实现**，
而不是在测试里重写一遍公式。

这个教训很实际：第一版测试自己抄了一份控制律，后来主程序把符号方向改对了、
测试里那份没改，于是测试"通过"但测的是过期逻辑。
测试和实现各写一遍 = 两个都不可信。
"""

import struct

# ---------------- 9000 网关协议（与 lab/agent/rkllm_embodied_agent.cpp 完全一致）----------------
MAX_BBOXES = 10
HEADER_FMT = "<II"                       # uint32 frame_id, uint32 bbox_count
BBOX_FMT = "<ifffff"                     # int32 class_id, float score, x, y, w, h
HEADER_SIZE = struct.calcsize(HEADER_FMT)          # 8
BBOX_SIZE = struct.calcsize(BBOX_FMT)              # 24
FRAME_SIZE = HEADER_SIZE + MAX_BBOXES * BBOX_SIZE  # 248

COCO80 = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
    "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]


def parse_frame(data, img_w, img_h):
    """解析 248 字节定长帧 → (frame_id, [bbox dict...])。坐标归一化到 [0,1]。"""
    if len(data) < FRAME_SIZE:
        raise ValueError("短帧: %d < %d" % (len(data), FRAME_SIZE))
    frame_id, count = struct.unpack(HEADER_FMT, data[:HEADER_SIZE])
    if count > MAX_BBOXES:
        return frame_id, []        # 畸形帧，丢弃（严禁越界读）
    out = []
    for i in range(count):
        off = HEADER_SIZE + i * BBOX_SIZE
        cid, score, x, y, w, h = struct.unpack(BBOX_FMT, data[off:off + BBOX_SIZE])
        out.append({
            "class_id": cid,
            "score": score,
            "x": x / img_w,
            "y": y / img_h,
            "w": w / img_w,
            "h": h / img_h,
            "cx": (x + w / 2.0) / img_w,
            "cy": (y + h / 2.0) / img_h,
        })
    return frame_id, out


def build_frame(frame_id, boxes):
    """打包一帧（PC 端模拟网关用）。boxes 为 (cid, score, x, y, w, h) 列表。"""
    assert len(boxes) <= MAX_BBOXES
    buf = struct.pack(HEADER_FMT, frame_id, len(boxes))
    for b in boxes:
        buf += struct.pack(BBOX_FMT, *b)
    buf += b"\x00" * (FRAME_SIZE - len(buf))
    return buf


def recv_full_frame_plain(sock, timeout=0.5):
    """收满 248 字节才算一帧（最朴素的版本，供诊断脚本用）。

    必须循环收：TCP 是字节流，单次 recv 只保证"给你一部分"，不保证"给你全部"。
    直接 recv(248) 然后解析，网络抖一下拿到的就是半帧乱码。

    所有错误收敛成 None——接收侧不该因对端断开就整个崩掉。
    """
    import socket as _socket
    buf = bytearray()
    sock.settimeout(timeout)
    while len(buf) < FRAME_SIZE:
        try:
            chunk = sock.recv(FRAME_SIZE - len(buf))
        except (_socket.timeout, ConnectionResetError, ConnectionAbortedError, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def pick_target(bboxes, want_class, min_score):
    """挑追踪目标：优先指定类别，同类里取面积最大。

    为什么按面积不按分数：画面里可能同时有几个人，面积大 = 离得近 = 用户大概率指的那个；
    而相邻目标的分数常常很接近，排序不稳定。
    """
    cands = [b for b in bboxes if b["score"] >= min_score]
    if want_class is not None:
        cands = [b for b in cands
                 if 0 <= b["class_id"] < len(COCO80) and COCO80[b["class_id"]] == want_class]
    if not cands:
        return None
    return max(cands, key=lambda b: b["w"] * b["h"])


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def compute_control(target_cx, target_cy,
                    pan, tilt,
                    kp_pan, kp_tilt,
                    fov_pan, fov_tilt,
                    pan_range, tilt_range,
                    deadzone, max_step,
                    invert_tilt=False):
    """由目标在画面中的位置算出新的 pan/tilt。

    返回 (new_pan, new_tilt, in_deadzone, dx, dy)

    符号约定（搞反了云台往反方向跑，表现得像"抽风"）：
      dx > 0 = 目标在画面右侧 = 目标在相机当前朝向的右边
              → 相机要向右转 → pan 增大          → d_pan = +err_pan
      dy > 0 = 目标在画面下方 = 目标在视轴下方
              → 相机要低头 → tilt 减小（约定正 tilt = 抬头）→ d_tilt = -err_tilt
    物理上"向右转"对应舵机往哪边，由固件 PAN_INVERT 兜底，这一层只管语义方向。

    先把像素偏差换成角度偏差再乘 kp，好处是 kp 有物理意义（0.5 = 每度偏差补 0.5 度），
    换摄像头/换分辨率不用重新瞎调。
    """
    dx = target_cx - 0.5
    dy = target_cy - 0.5

    if abs(dx) < deadzone:
        dx = 0.0
    if abs(dy) < deadzone:
        dy = 0.0
    if dx == 0.0 and dy == 0.0:
        return pan, tilt, True, dx, dy

    err_pan_deg = dx * fov_pan
    err_tilt_deg = dy * fov_tilt

    d_pan = err_pan_deg * kp_pan
    d_tilt = -err_tilt_deg * kp_tilt
    if invert_tilt:
        d_tilt = -d_tilt

    # 单帧限速：目标突跳时云台不猛甩（机械冲击 = 扫齿风险）
    d_pan = clamp(d_pan, -max_step, max_step)
    d_tilt = clamp(d_tilt, -max_step, max_step)

    new_pan = clamp(pan + d_pan, -pan_range, pan_range)
    new_tilt = clamp(tilt + d_tilt, -tilt_range, tilt_range)
    return new_pan, new_tilt, False, dx, dy


def compute_control_absolute(target_cx, target_cy,
                             pan, tilt,
                             pan_range, tilt_range,
                             gimbal_fov_pan, gimbal_fov_tilt,
                             fov_pan, fov_tilt,
                             camera_pan=0.0, camera_tilt=0.0,
                             deadzone=0.05, max_step=6.0):
    """**摄像头固定不动**时的控制律：把画面位置直接映射成云台绝对角度。

    ── 什么时候用这个，而不是 compute_control ──────────────────────
    两个控制律的前提完全不同，混用会有一路"看起来能动、其实永远收敛不了"：

    | | compute_control（增量式） | compute_control_absolute（绝对式） |
    |---|---|---|
    | 前提 | **摄像头装在云台上**，跟着转 | **摄像头固定**，云台单独转 |
    | 输入 | 目标相对画面的偏差 dx/dy | 目标在画面中的绝对位置 cx/cy |
    | 输出 | 在当前角度上**累加**增量 | 直接算出**目标角度** |
    | 装错摄像头会怎样 | dx 恒定不变 → 一路累加到限位卡死 | —— |

    实测踩过的坑：摄像头固定时用增量式，`dx` 恒为 0.103，
    pan 从 2.8 一路累加到 90° 顶死限位。不是控制律写错了，
    是**它的前提不成立**。

    ── 几何推导 ───────────────────────────────────────────────
    摄像头固定、朝向某个基准方向；云台也是独立轴的。要让云台"指向"画面里的目标：

      目标相对相机视轴的夹角  α_cam = (cx - 0.5) * fov_cam      ← 画面位置换算
      目标在世界中的方位角    θ_world = camera_pan + α_cam       ← 加相机自身朝向
      云台应该转到的角度      pan_new = θ_world - gimbal_ref     ← 减去云台零位

    这里约定云台零位 = 相机光轴方向（也就是"云台正对相机视野中心"），
    所以 camera_pan=camera_tilt=0 时简化为：
      pan_new = (cx - 0.5) * fov_pan
      tilt_new = -(cy - 0.5) * fov_tilt

    **注意用的是云台的 FOV(fov_pan)，不是相机的 FOV。**
    云台需要转过的角度由"目标在相机画面里的位置 × 相机视场角"决定，
    再除以云台自己的行程范围。这里 fov_pan/fov_tilt 是**相机**视场角，
    gimbal_fov_pan/gimbal_fov_tilt 用于把世界角度压缩到舵机行程内。

    ── 参数说明 ───────────────────────────────────────────────
    pan, tilt           : 当前云台角度（用于 max_step 限速，不让它一步跳很远）
    pan_range/tilt_range: 舵机行程（±90 / ±45）
    gimbal_fov_pan/tilt : 云台覆盖的视野角（通常等于相机 FOV，因为云台转到哪就看到哪）
    fov_pan/fov_tilt    : 相机自身视场角，用来把归一化位置换成角度
    camera_pan/tilt     : 相机相对世界基准的朝向偏移（默认 0 = 相机正对"前方"）
    deadzone            : 角度死区内不动（用**归一化位置**判断，不是角度）
    max_step            : 单帧最大角度变化

    返回 (new_pan, new_tilt, in_deadzone, dx, dy)
    """
    dx = target_cx - 0.5
    dy = target_cy - 0.5

    if abs(dx) < deadzone:
        dx = 0.0
    if abs(dy) < deadzone:
        dy = 0.0

    # ★ 死区只作用于**画面偏差**，不能提前 return。
    #
    # 第一版这里写的是 `if dx == 0 and dy == 0: return pan, tilt, True`，
    # 结果是：目标正好在画面正中、而相机朝向 30° 时，云台直接返回 0° ——
    # camera_pan 被死区短路掉了。测试第 6.5 条抓到的就是这个。
    #
    # 语义上"死区"是"画面这一点点偏移不值得动"，它管的是 alpha（相对相机视轴），
    # 不是"云台最终该指哪里"。camera_pan 是绝对基准，跟死区无关。

    # 目标相对相机视轴的角度（相机 FOV 决定"画面边缘 = 多少度"）
    alpha_pan  = dx * fov_pan
    alpha_tilt = dy * fov_tilt

    # 加上相机自身朝向 → 世界方位角；再减去云台零位 → 云台目标角
    want_pan  = camera_pan + alpha_pan
    want_tilt = camera_tilt - alpha_tilt      # 画面下方 = 世界低处；约定正 tilt = 抬头

    # 把世界角度压进舵机行程：超出部分按比例缩放，避免要它转 120° 却只有 90° 可用
    if gimbal_fov_pan > 0 and abs(want_pan) > gimbal_fov_pan / 2.0:
        scale = (gimbal_fov_pan / 2.0) / abs(want_pan)
        want_pan *= scale
    if gimbal_fov_tilt > 0 and abs(want_tilt) > gimbal_fov_tilt / 2.0:
        scale = (gimbal_fov_tilt / 2.0) / abs(want_tilt)
        want_tilt *= scale

    want_pan  = clamp(want_pan,  -pan_range,  pan_range)
    want_tilt = clamp(want_tilt, -tilt_range, tilt_range)

    # 死区判定放在算出目标角**之后**：画面偏差小、且云台已经指在目标角上，
    # 才真的不需要动。少了这个判据会让云台在死区边缘一直微抖。
    ang_dz_pan  = deadzone * fov_pan
    ang_dz_tilt = deadzone * fov_tilt
    if dx == 0.0 and dy == 0.0 \
            and abs(want_pan - pan) <= ang_dz_pan \
            and abs(want_tilt - tilt) <= ang_dz_tilt:
        return pan, tilt, True, dx, dy

    # 限速：从当前角度朝目标角走，单帧最多走 max_step
    d_pan  = clamp(want_pan  - pan,  -max_step, max_step)
    d_tilt = clamp(want_tilt - tilt, -max_step, max_step)

    new_pan  = clamp(pan  + d_pan,  -pan_range,  pan_range)
    new_tilt = clamp(tilt + d_tilt, -tilt_range, tilt_range)
    return new_pan, new_tilt, False, dx, dy
