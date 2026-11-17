# 云台追踪版本清单

> 建立时间：2026-11-17 09:25
> 板上路径：`/root/lab/`
> 命名规则：`v<序号>_<功能特征>`

---

## 一、四个正式版本

| 版本 | 文件 | 大小 | 包含的改动 | 性能 |
|---|---|---|---|---|
| **v1** | `v1_原始_无优化` | 116080 B | 无（11-16 14:19 的基线） | 26 FPS / 39ms 延迟 |
| **v2** | `v2_tiltfix_分轴` | 115928 B | tilt 分轴限幅 + 变步长 + `invert_tilt` 修正 + `tgt_cy` 改框顶边 | 28.4 FPS / 39ms |
| **v3** | `v3_流水线` | 130184 B | v2 + 三线程流水线 | 30 FPS / **22.4ms** |
| **v4** | `v4_流水线_P1_P3` | 130184 B | v3 + 帧间插值 `S 40` + 异步回包 | 30 FPS / 22.2ms |

**v4 是当前正式版。**

---

## 二、各版本解决的具体问题

### v1 —— 基线

四项优化之前的状态。已知问题：
- tilt 和 pan 共用 `max_step`，tilt 单帧 48px > 死区 32px → 蹲起后摆好几轮
- `invert_tilt = true` 方向反 → tilt 一路顶到限幅（镜头朝天）
- `tgt_cy` 用框高计算 → 蹲起时框高一变 `cy` 就跳 54px
- 串行处理，每帧 39ms

### v2 —— tiltfix（用户确认的版本）

**核心修复：分轴限幅**

```cpp
float max_step_pan  = 0.6f;    // 42.7 px/单位
float max_step_tilt = 0.20f;   // ★ 80.0 px/单位，必须单独设
float deadzone_pan  = 0.025f;
float deadzone_tilt = 0.035f;
```

**为什么必须分轴**：

```
固件量程: PAN ±90 指令=±90°物理, TILT ±45 指令=±90°物理
→ tilt 1 单位 = 4°物理 = 80px, pan 1 单位 = 2°物理 = 42.7px

共用 max_step=0.6:
  pan  单帧 25.6px < 死区 32px  ✓
  tilt 单帧 48.0px > 死区 32px  ✗ 每步冲出死区 → 极限环
```

配套修复：
- `invert_tilt = false`（原来是 true，正反馈）
- `tgt_cy = y1 + rows*0.10`（原来 `y1 + bh*0.18`，被框高带偏）

**实测效果**：tilt 轨迹 `T-14.3 → -14.3 → -14.3 → -14.0（连续 10 帧不动）` —— 一次到位。

### v3 —— 流水线

三线程架构（采集/推理/显示），队列容量 1 + 覆盖丢旧。

**核心收益**：控制延迟 39 → 22.4ms（−43%）
**帧率**：26 → 30 FPS（撞相机硬件上限）

真实价值体现在视频输入：34 → 55 FPS。

### v4 —— P1 + P3

**P1 帧间插值 `S 40ms`**
舵机默认"收到新角度就跳过去"，改成一帧时长的指数缓动。
实测：观察到 8 个中间值 `13.8→21.8→27.4→31.2→33.9→35.8`。

**P3 异步回包**
原来阻塞 `read()` 等回包（RTT p50=19.2ms，占单帧一半），改成 `select` 2ms 超时。
实测：B 阶段 20.4ms = NPU 17.3 + 解码 3.1，回包耗时归零。

---

## 三、源码对应关系

| 源码 | 行数 | 对应版本 | 位置 |
|---|---|---|---|
| `w4_edgevision_gimbal_preview.cpp` | 2967 | **v4（当前）** | `/root/lab/` + git 仓库 |
| `_archive_20261116/w4_edgevision_gimbal_preview.v2.cpp` | 2141 | **v2（tiltfix）** | 板上存档 |
| `_archive_20261116/w4_edgevision_gimbal_preview.cpp` | 1961 | v1 → v2 过渡（`tuned`） | 板上存档 |

**⚠️ 注意**：板上源码和 git 仓库源码是两份，需手动同步。当前已同步（md5 一致）。

---

## 四、如何切换版本

### 临时测试某个版本

```bash
ssh rk3588 "cd /root/lab && ./v2_tiltfix_分轴 /dev/video83 --no-3d"
```

### 正式切换（改服务）

```bash
ssh rk3588 "sudo systemctl stop gimbal-tracker
cd /root/lab
sed -i 's|v4_流水线_P1_P3|v2_tiltfix_分轴|' /etc/systemd/system/gimbal-tracker.service
sudo systemctl daemon-reload
sudo systemctl start gimbal-tracker"
```

### 查看当前服务用的是哪个版本

```bash
ssh rk3588 "grep ExecStart /etc/systemd/system/gimbal-tracker.service"
```

---

## 五、git 中的版本标记

| tag | 指向 | 含义 |
|---|---|---|
| `gimbal-before-opt` | `af9d098` | 四项优化前（相当于板上 v1） |
| `gimbal-opt-v1` | `5f17e10` | 四项优化完成（相当于板上 v4 的代码） |

**注意**：git tag 标记的是**源码状态**，不是二进制。板上二进制需要单独管理（见本文档）。

---

## 六、命名变更记录

2026-11-17 09:23 理顺命名：

| 旧名 | 新名 | 说明 |
|---|---|---|
| `w4_edgevision_gimbal_preview.v1_原始` | `v1_原始_无优化` | 移到 `/root/lab/` 根目录 |
| `w4_edgevision_gimbal_preview.v2` | `v2_tiltfix_分轴` | 用户确认的 tiltfix 版 |
| `w4_edgevision_gimbal_preview.pipe` | `v3_流水线` | 纯流水线，无 P1/P3 |
| `w4_edgevision_gimbal_preview.final` | `v4_流水线_P1_P3` | **当前正式版** |

旧命名文件已备份到 `_archive_20261116/pre_rename/`。

---

## 七、下一步：P2 固件（未烧录）

`main/gimbal_main.c` 已改成 **PWM 50Hz → 333Hz + 控制循环 20ms → 3ms**，
但**板上跑的仍然是 50Hz 旧固件**。

烧录方法（ESP32 插 PC）：

```powershell
cd D:\a研究生\项目\rk3588项目\rk3588-edge-ai\esp32_gimbal
pwsh -File build.ps1 -Flash
```

烧完验证：

```bash
ssh rk3588 "bash /root/esp32_gimbal/verify_after_flash.sh"
```

预期：串口往返延迟 `p50` 从 **19.87ms → ~1.5ms**。
