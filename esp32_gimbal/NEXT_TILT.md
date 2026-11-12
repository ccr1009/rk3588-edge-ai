# 接上第二颗 tilt 舵机后的操作清单

> 写于 2026-11-03 深夜。当前状态：Pan 轴（GPIO14）已通，
> Tilt 轴（GPIO15）空着，只等电源到位接线。
>
> **为什么必须接 tilt**：现在人离摄像头 <0.5m，检测框占画面 79%，
> 云台转到任何角度身体都填满画面 → 闭环不成立 → pan 必然撞限位。
> 接上 tilt 后云台能抬头，画面能装下整个人，闭环才成立。
> 这一节把"接上之后要做什么"按顺序写清楚，照着做即可。

---

## 步骤 0：接线（安全第一）

### 0.1 供电必须独立

```
独立 5V 2A 电源（手机充电头 or LM2596S 从 12V 降压）
   ├── Pan  舵机 红线
   ├── Tilt 舵机 红线
   └── 两个舵机 棕/黑线 ──┐
                          ├── 公共 GND
USB转TTL 模块 GND ────────┤
ESP32 GND ────────────────┘
```

⚠️ **绝对不要**：把两颗舵机都挂到 USB-TTL 模块的 5V 上。
实测证据：CH340 和 UVC 摄像头曾**同一秒掉线**，`error -71`——
因为 SG90 堵转 1.4A/颗，两颗逼近 3A，板子 USB 口只给 500mA。

### 0.2 信号线

| 舵机 | 信号线接到 | 说明 |
|:---|:---|:---|
| Pan | ESP32 **GPIO14** | 已验，不动 |
| Tilt | ESP32 **GPIO15** | 固件默认值 |

如果不方便接 GPIO15（比如排针位置不好插），固件支持**运行时改引脚**：
```bash
# 串口发 T <pin> 即可，不用重烧
T 16
```

### 0.3 接线后第一步：确认舵机有自锁力矩

```bash
# 回中，然后用手拨 tilt 舵机的摇臂
cd /root/esp32_gimbal
/root/rknn_env/bin/python gimbal_probe.py --port /dev/ttyUSB0
```

| 手感 | 含义 |
|:---|:---|
| **捏不动 / 松手回弹** | 电 + PWM 都到位 ✅ |
| **轻轻一拨就转** | 缺电或缺信号 ❌ 回去查供电 |

---

## 步骤 1：确认 tilt 舵机真的会动（不跑追踪）

```bash
# 停掉一体化程序（它占着串口）
bash /root/lab/recover.sh

# 手动让 tilt 抬头低头，用眼睛看
/root/rknn_env/bin/python - <<'PY'
import serial, time
s = serial.Serial("/dev/ttyUSB0", 115200, timeout=1.0)
time.sleep(0.3); s.reset_input_buffer()
for t in (-30, 0, 30, 0):
    s.write(("P 0 %d\n" % t).encode()); s.flush(); time.sleep(1.2)
    print("tilt=%+3d ->" % t, s.read(200).decode(errors="replace").strip())
s.close()
PY
```

**判据**：摄像头应该**跟着抬头 → 低头 → 回中**。
如果完全不动 → 查 GPIO15 接线 / 用 `I` 命令确认 `GPIN 14 15`。

---

## 步骤 2：标定 tilt 方向（★ 必做，不要跳过）

```bash
# 先起检测流（纯预览模式，不驱动云台）
cd /root/lab
nohup ./w4_edgevision_gimbal_preview /dev/video81 > /tmp/gp.log 2>&1 &
sleep 8

# 人站到 1.5~3m，能框到上半身
/root/rknn_env/bin/python /root/esp32_gimbal/calibrate_tilt.py
```

**判据**（脚本会自动给结论）：

| 结果 | 含义 | 要不要加 `--invert-tilt` |
|:---|:---|:---|
| `tilt↑ → cy↓` | 负反馈，方向正确 | ❌ 不用 |
| `tilt↑ → cy↑` | 正反馈，方向反了 | ✅ 要加 |

> 复盘一下 pan 是怎么标定的（`calibrate_direction.py`）：
> 三位置 × 15 帧，`cx = 772 / 928 / 1022`，`Δcx=+249.7` → pan 反。
> tilt 用同样的方法，只是看 `cy`。

---

## 步骤 3：真闭环（这次能收敛了）

### 3.1 先跑「只有 pan」确认不撞限位

```bash
bash /root/lab/restart.sh --no-tilt --no-3d
# 浏览器看 http://<板子IP>:8080/
```

**判据**：`pan` 应该在 ±40° 内跟随，状态在 `TRACK` / `LOCK` 之间切换，
**不应出现 `SATURATED`**（顶限位）。

如果这时还撞限位 → 人还是太近，或者摄像头视野太窄，先别接 tilt 往下走。

### 3.2 再上双轴

```bash
# 根据步骤 2 的结论决定要不要 --invert-tilt
bash /root/lab/restart.sh --no-3d            # 方向正确的情况
bash /root/lab/restart.sh --no-3d --invert-tilt   # 方向反了的情况
```

**双轴成功判据**：

| 指标 | 期望 |
|:---|:---|
| 状态 | 反复出现 `LOCK`（进死区） |
| `pan` 范围 | 不贴 ±90 |
| `tilt` 范围 | 不贴 ±45 |
| `errors` | 0 |
| 视觉 | 你的人在中位十字附近，抬头低头能跟上 |

---

## 步骤 4：验证后要更新的文档

1. `docs/CURRENT_STATUS.md` —— 双轴闭环通了
2. `docs/BENCHMARKS.md` —— 记两个数字：双轴 FPS、稳态误差
3. 本文档 —— 把实测数据填进来（tilt 标定表、pan/tilt 范围）

---

## 附：为什么"不撞限位"是判断闭环成立的硬指标

如果 `pan` 总是停在 ±90（`SATURATED`），说明**目标始终在视野外**，
控制律永远认为"还没到位"→ 一直推。可能原因按概率排：

| 可能 | 判据 | 解法 |
|:---|:---|:---|
| 人太近，框占满画面 | 框宽/画面宽 > 0.7 | 退远 or 接 tilt 抬头 |
| 方向真的反了 | `SATURATED` 且目标在反方向 | 重跑标定 |
| 摄像头没装在云台上 | 转云台画面不变 | 检查安装 |
| tilt 顶死拖累判据 | 但 pan 也不该因此撞墙 | 分离两轴测试 |

**2026-11-03 实测：这条一直触发，最终定位到"人太近"**——
框占画面 79%（2D 检测框 1014×698），而不是之前误判的"框很小"
（那个小框是 3D 反投影算错的，`--no-3d` 关掉后能看到真实框）。
