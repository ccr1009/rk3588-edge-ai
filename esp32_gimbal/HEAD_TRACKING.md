# 追头闭环：从「追不紧」到「能锁定但受几何约束」

> 2026-11-16 晚，两个子智能体（cpp-patcher / perf-analyst）并行工作 + 主控实测。
> 本文记录**有效结论**和**踩过的坑**，都是实测数据，不是推测。

---

## 一、结论摘要

| 项目 | 状态 |
|---|---|
| C++ 版帧率 | **25.6 FPS**（每帧 39.1ms），不是 23.4 |
| 追头逻辑 | ✅ 已实现（从 person 框推算头部，绕开"腿被裁"） |
| 过滤开关 | ✅ 已修（原 `--no-target-filter` 是假开关，永远打不开） |
| 方向符号 | ✅ 已修（带 `--rotate 180` 时必须 invert_pan=false） |
| 参数 | ✅ 已按推导重设（kp=0.20/0.13, deadzone=0.025, max_step=2.5） |
| 实际锁定 | ✅ 能锁定（`sent` 0→67，`locked:true` 出现过，`errors:0`） |
| **跟紧** | ⚠️ **受几何约束限制**，见第五节 |

---

## 二、必须记住的三个坑（按价值排序）

### 坑 1：控制律是积分型，不是位置式

```cpp
out_pan = pan + kp * (cx-0.5) * FOV    // 每帧累加到当前实际位置
```

即 `d(pan)/dt = kp·err/T_frame`，开环传递函数 `L(s) = K·e^(-sTd)/(s(1+τs))`，**其中 K = kp/T_frame**。

**这意味着同样的 kp 在不同帧率下行为完全不同。** 我们在 2 FPS 的 Python 脚本上用 kp=0.5 跑得不错，搬到 25.6 FPS 会直接振荡（仿真：过冲 8.9°、过零 7 次）。帧率差了 12.8 倍。

> 教训：调 PID 前先确认控制律形式和帧率，不能拿"上次能用"的值直接套。

### 坑 2：`--rotate 180` 会翻转符号

```cpp
--rotate 180  →  cv::flip(bgr, rot, -1)   // x/y 同时镜像
              →  cx_rotated = W - cx_raw
```

镜像一次 → 符号翻一次。所以：

| 启动方式 | invert_pan |
|---|---|
| 不带 `--rotate` | `true` |
| 带 `--rotate 180`（本机实际） | **`false`** |

我们之前"目标偏右时 pan 必须减小"的实测结论（直接读原始帧，没 flip）是对的，但**不能直接套到开了 rotate 的程序上**。现已实现为按 `g_rotate` 自动推导，避免下次换启动参数就飞。

### 坑 3：`--no-target-filter` 是个假开关

```cpp
bool filter_partial = false;                              // 默认关
else if (strcmp(argv[i], "--no-target-filter") == 0)
    cfg.filter_partial = false;                           // ← 只是再设一次 false
```

**全程序没有任何办法把过滤打开。** 所以之前 `check_filter.py` 离线推算的"10/10 帧被拒"不能代表 C++ 实际行为 —— 旧二进制跑起来过滤一直是关的。

已补 `--target-filter` 并**默认开启**，否则这次改动根本没机会生效。

---

## 三、追头的判据（核心思路）

### 问题：追身体会误杀所有近距离目标

实测人坐镜头前 1 米，框是 `(573, 464, 1006, 716)`：

```
y1=464  ← 头顶，离画面顶 464px（头完整）
y2=716  ← 画面底 720，只差 4px（腿被裁）
```

旧判据 `touches_edge = clip_left || clip_right || clip_top || clip_bot` 因为 `clip_bot` 成立就拒绝。**但腿被裁完全不影响头的水平位置。**

### 解法：只信头，不信身体

```cpp
const bool head_bad = clip_top || clip_left || clip_right;   // ★ 底边故意不在内
```

| 边 | 处理 | 理由 |
|---|---|---|
| 顶边贴边 | ❌ 拒绝 | 头被切，追了没意义 |
| 左右贴边 | ❌ 拒绝 | 头横向被裁，x 不可信 |
| **底边贴边** | ✅ **放行** | 腿被裁不影响头 |

头部位置推算（COCO 无 head 类）：

```cpp
const float HEAD_Y_RATIO = 0.18f;
tgt_cx = (ox1 + ox2) * 0.5f / cols;
tgt_cy = (oy1 + (oy2 - oy1) * HEAD_Y_RATIO) / rows;   // 原来是框中心
```

`ASPECT_MAX` 从 1.20 放宽到 3.00（坐姿近距离人框 aspect 可达 1.76，旧值会误杀）。

判据离线测试 13/13 PASS，含边界：顶边 14px 拒 / 15px 放行（阈值 14.4px）。

---

## 四、参数为什么是这些值（不是拍脑袋）

### 舵机是绝对主导延迟

| 步进 | 舵机运动 | 端到端总延迟 |
|---|---|---|
| 10° | 218 ms | **≈282 ms** |
| 20° | 252 ms | ≈316 ms |
| 30° | 352 ms | ≈416 ms |

拟合 `t ≈ 140ms + 6.7ms/deg`，等效 **46~85 °/s**。
DS3120 标称 0.17s/60° @6.8V，实际供电 5V 更慢。

**舵机运动 218ms 是整个处理链 39.1ms 的 5.6 倍。** 反馈延迟 Td≈88ms 里舵机惯性占绝大部分 —— 这就是 kp 不能大的物理原因，调软件突破不了。

### 死区是"跟不紧"的真元凶

```cpp
dz_scale 自适应：人近、框大时放大到 1.29~1.50
deadzone 0.05 × 1.5 = 0.075 → 角度死区 3.9~4.5°
→ 1 米处横向盲区 ±70~79cm
```

**人走几步云台才动**，这才是"跟不紧"的直接原因，不是增益不够。降到 0.025 后盲区缩到 ±28~47cm。

> 下限约束：近距离框中心抖动可达 ±20px ≈ 0.94°，所以 `deadzone ≥ 0.017`，否则噪声被放大成持续抖动。

### 最终值

| 参数 | 值 | 依据 |
|---|---|---|
| kp_pan | 0.20 | 临界 kp_u=0.37~0.75，取 45~60% |
| kp_tilt | 0.13 | 垂直 FOV 36°<水平 60°，且 tilt 有重力负载、安全范围仅 ±35° |
| deadzone | 0.025 | 见上；不低于 0.017 |
| max_step | 2.5 | 物理上限 3.3°/帧（39.1ms × 85°/s），超了指令跑在机械前面 |

**不要加阻尼/低通滤波**：Td 只有 2.2 帧，收益小、代价是变慢。`--smooth` 保持默认关。

---

## 五、⚠️ 未解决：几何约束（不是参数问题）

### 现象

新程序能锁定，但在 `TRACK` / `NO_GOOD_TGT` 之间来回切：

```
[profile] ... | P-7.9 T-22.5 TRACK        | 拒绝框 1
[profile] ... | P-7.9 T-20.6 NO_GOOD_TGT  | 拒绝框 1
[profile] ... | P-26.2 T-21.1 TRACK       | 拒绝框 0
[profile] ... | P-27.1 T-25.5 LOST        | 拒绝框 0
```

### 根因：头完整可见的角度 和 头居中的角度 不重合

实测标定（pan=0，逐 tilt 扫）：

| tilt | 顶边 y1 | 框面积 | 头状态 |
|---|---|---|---|
| -40 ~ -20 | **0~5** | 56~62% | ❌ 头被切 |
| **-15** | **288** | 21% | ✅ **头完整**，但头在画面偏下 |
| -10 | 561 | 6% | ✅ 头完整但太小 |

追头想把头推到画面中心 → 必须让 tilt 转到 -21 附近 → **但那个角度头就被切了** → `TOP_CUT` 拒绝 → 摆回去 → 再推 → 振荡。

**这是摄像头装在云台上方/下方的几何后果，不是代码 bug，也不是参数没调好。**

### pan 方向同样受限

纯 pan 追踪实测 60 帧：
```
检出 56(93%)，动作 5 次，拒(头切 8 / 侧裁 40)
|errX| 中位=0.184，最小=0.002
前5帧均值 0.113 → 末5帧 0.172   ⚠️ 未收敛
```
48/60 帧被拒，其中 **侧裁 40 帧占大头** —— 人左右移动时框频繁超出左右边界。

### 三条可能的出路

| 方案 | 代价 | 效果 |
|---|---|---|
| **人退到 2~3 米** | 需要空间 | 框变小，头和身体都能装进画面 → 约束消失 |
| **换广角镜头** | 花钱，~¥30 | 视野够大，近距离也能装下 |
| **只追 pan，tilt 手动锁死** | 已实现 | 稳定但只有水平跟随 |

> **结论：想要"跟紧"，必须先解决几何问题，再谈调参。** 这也印证了 README 8.7.5 记录的历史结论 —— 那时"冲限位"的根源同样是闭环在物理上不成立。

---

## 六、perf-analyst 附带发现的真 bug

程序 **SIGTERM 杀不死**：

```cpp
pthread_cond_broadcast(&g_jpeg_cond);
pthread_join(http_tid, nullptr);   // ← HTTP 线程阻塞在 accept()，g_running=0 唤不醒
```

信号处理只置 `g_running = 0`，但 HTTP 线程卡在阻塞 `accept()`（`wchan = inet_csk_accept`）永不返回 → `pthread_join` 永久阻塞 → **进程变僵尸，8080 一直被占**。

- 临时对策：测试一律用 `timeout -s KILL`
- 根修：给 accept 加 `select`/`poll` 超时，或信号处理里 `shutdown(server_fd, SHUT_RDWR)`

**未修**（不影响追踪，但下次调试会再踩）。

---

## 七、文件清单

| 文件 | 状态 |
|---|---|
| `/root/lab/w4_edgevision_gimbal_preview.cpp` | ✅ 已改 |
| `/root/lab/w4_edgevision_gimbal_preview.new` | ✅ 新二进制（116000B，md5 74dfe7ba…），**未替换在用版本** |
| `/root/lab/w4_edgevision_gimbal_preview.cpp.bak_195309` | 备份（md5 与原文件一致） |
| `host/head_track_verify.py` | 验证脚本（可复现 pan 收敛数据） |
| `host/recalibrate_pose.py` | 中位漂移后重新找"能看见人"的角度 |

### 切换新版本

```bash
cd /root/lab
cp w4_edgevision_gimbal_preview w4_edgevision_gimbal_preview.old   # 留旧版
cp w4_edgevision_gimbal_preview.new w4_edgevision_gimbal_preview
./w4_edgevision_gimbal_preview /dev/video83 --bridge 127.0.0.1:9100 --no-3d --rotate 180
```

### 验证要点

启动横幅应出现：
```
kp=(0.20,0.13) max_step=2.5 invert=(pan=0,tilt=1) filter=1 rotate=180
```

运行中用 `curl -s http://127.0.0.1:8080/status` 观察：
- `fps` ≈ 25.3（稳定）
- `errors` = 0（有错说明桥接或舵机异常）
- `sent` 持续增长说明在发指令
- `locked` 应为 true
