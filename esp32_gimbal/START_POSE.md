# 启动姿态参数（v5 新增）

> 2026-11-17 新增。解决「舵机固定后中位看不到人 → 往错误方向追到限位卡死」的问题。

---

## 一、问题背景

用户把舵机固定了（无法手动调整朝向，人也过不去，靠墙）。

**现象**：
- 上电中位是 `P0 T0`，镜头朝**天花板**
- 追踪程序启动时沿用当前姿态
- 看不到人就 `objects=0`，控制律往错误方向一直推
- 最终顶到 `P-90` 限位，**停在那里动不了**

实测日志（修复前）：
```
[+] 云台控制已启用 ...
[profile] ... P-90.0 T-5.6 LOST      ← 顶到限位
```

---

## 二、解决方案

新增两个启动参数：

```bash
--start-pan 5 --start-tilt -20
```

程序在连上云台后，**先摆到这个姿态，等 1.5 秒到位，再开始追踪**。

### 用法

```bash
./v5_startpose /dev/video83 --bridge 127.0.0.1:9100 --no-3d \
    --start-pan 5 --start-tilt -20
```

### 完整服务配置

```ini
[Service]
WorkingDirectory=/root/lab
ExecStart=/root/lab/v5_startpose /dev/video83 --bridge 127.0.0.1:9100 --no-3d --start-pan 5 --start-tilt -20
```

---

## 三、怎么找到合适的启动姿态

用 `scan_for_you.py` 扫一圈：

```bash
ssh rk3588 "/root/rknn_env/bin/python /root/esp32_gimbal/scan_for_you.py"
```

它会 pan 从 -85 到 +85、tilt 从 -35 到 -5 逐档扫，
每档跑 person 检测，告诉你哪个角度能检出人且**头完整**（`y1 > 14`）。

**本机实测结果**（2026-11-17 下午，光照明亮）：

| pan | tilt | conf | 框 | 判定 |
|---|---|---|---|---|
| **+5** | **-20** | 0.475 | `(615, 369, 937, 712)` | ✅ **头完整，推荐** |
| +5 | -25 | 0.871 | `(533, 59, 1261, 715)` | y1=59 偏上 |
| +5 | -30 | 0.833 | `(167, 3, 1251, 717)` | y1=3 头切 |
| +65 | -35 | 0.50 | 面积 3.4% | ✅ 可追但目标小 |

**为什么选 `P5 T-20`**：头完整（y1=369，离顶部还有 369px），框占画面 12%，大小适中。

> ⚠️ 中位 `P0 T0` 是朝天花板的。这不是 bug，是云台物理安装的朝向。

---

## 四、实现中踩的坑（重要）

### 坑：只摆姿态不改初值 → 白摆

第一版只在云台连上后发一条 `P 5 -20`，日志确认摆过去了：

```
[+] 启动姿态: P +5.0 -20.0（等待到位）
```

**但下一帧控制立刻把它拉回来了**：

```
[profile] P-1.6 T-5.0 LOST      ← tilt 从 -20 被拉到 -5（tilt_safe 上界）
```

**根因**：`thread_b_infer` 里

```cpp
float cur_pan = 0.f, cur_tilt = 0.f;    // ← 控制律以为当前在 (0,0)
```

控制律用 `cur_pan/cur_tilt` 作为"当前角度"算增量。姿态摆到 -20 了，
但程序以为在 0，第一帧算出"要从 0 修正到目标"，结果被 `tilt_safe=[-35,-5]` 钳到 -5。

**修法**：初值跟着启动姿态走

```cpp
float cur_pan  = isnan(px->cfg.start_pan)  ? 0.f : px->cfg.start_pan;
float cur_tilt = isnan(px->cfg.start_tilt) ? 0.f : px->cfg.start_tilt;
```

### 修复前后对比

```
修复前： [+] 启动姿态: P +5.0 -20.0
        P-1.6 T-5.0 LOST          ← 姿态被拉回，LOST

修复后： [+] 启动姿态: P +5.0 -20.0
        P+2.1 T-17.2 LOCK         ← 连续 6 帧稳定，LOCK
```

---

## 五、参数语义

| 参数 | 默认 | 说明 |
|---|---|---|
| `--start-pan N` | NAN（不启用） | 启动时先摆到这个 pan |
| `--start-tilt N` | NAN（不启用） | 启动时先摆到这个 tilt |

**只给一个也可以**，另一个会当作 0：

```bash
--start-tilt -20            # pan 用 0
--start-pan 5 --start-tilt -20   # 两个都指定
```

**都不给**（默认）：沿用当前姿态，保持向后兼容。

---

## 六、相关文件

| 文件 | 说明 |
|---|---|
| `/root/lab/v5_startpose` | 带启动姿态的二进制（130160 B） |
| `/root/lab/w4_edgevision_gimbal_preview.cpp` | 源码（补丁已合入，不再需要单独的补丁脚本） |
| `host/scan_for_you.py` | 扫描找合适姿态的工具 |
| `/etc/systemd/system/gimbal-tracker.service` | 已配置启动姿态 |

---

## 七、丢目标回位（v6 新增，2026-11-17）

### 问题

启动姿态解决了"一开始能看到人"，但人离开画面后：

```
看不到 → has_target=false → 控制律不动作 → 云台停在原地
```

如果之前追错方向已经推偏（实测顶到 P-90），人回来也找不到。

### 解决

连续 `lost_home_frames` 帧（默认 30 ≈ 1.2 秒 @25fps）没有可追目标时，
**回到启动姿态**。

```bash
--lost-home-frames 30     # 0 = 不启用
```

### 为什么回到"启动姿态"而不是"中位 P0 T0"

中位是朝天花板的，回去也是白回。启动姿态是确认过"能看到人"的角度。

### 三道保护

1. `lost_home_frames == 0` 时完全不启用（向后兼容）
2. 没设启动姿态时回到中位（否则没地方可回）
3. `homed_after_lost` 标志防止每帧重复发（省串口带宽、少舵机磨损）

### 实测验证

```
[+] 启动姿态: P +5.0 -20.0（等待到位）
P+0.7 T-16.7 TRACK              ← 正常追踪
P+13.6 T-16.6 TRACK
P+19.7 T-19.7 TRACK
[gimbal] 丢目标 30 帧，回到启动姿态 P +5.0 -20.0    ← ★ 回位触发
P+1.8 T-15.7 LOCK               ← 重新锁定
P+6.8 T-12.2 TRACK
```

### 实现中的坑

`lost_frames` 的自增必须放在 `if` 外面只做一次。第一版写成：

```cpp
if (++lost_frames > N && cfg.lost_home_frames > 0 && !homed) { ... }
```

条件短路会导致自增次数不确定。改成：

```cpp
++lost_frames;                      // 先累加，只做一次
if (lost_frames > 30) { ... }       // 原语义
if (cfg.lost_home_frames > 0 &&
    lost_frames > cfg.lost_home_frames &&
    !homed_after_lost) { ... }      // 回位
```

---

## 八、完整服务配置（v6）

```ini
[Service]
WorkingDirectory=/root/lab
ExecStart=/root/lab/v6_home /dev/video83 --bridge 127.0.0.1:9100 --no-3d --start-pan 5 --start-tilt -20 --lost-home-frames 30
KillMode=mixed
KillSignal=SIGKILL
TimeoutStopSec=5
```

### 版本对照

| 版本 | 功能 |
|---|---|
| v4 | 流水线 + P1 + P3 |
| v5 | + 启动姿态（`--start-pan/--start-tilt`） |
| **v6** | **+ 丢目标回位（`--lost-home-frames`）** |
