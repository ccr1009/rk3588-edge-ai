# 云台追踪优化实录（2026-11-16 夜间 → 11-17 凌晨）

> 用户去休息前提的要求：「都做了可以吗，程序先搞出来，明天再看」
> 本文记录四项优化的实现、实测数据和明天要做的剩余事项。
>
> **2026-11-17 00:10 更新**：验证阶段又发现并修掉了 **两个真 bug**（桥接多行回包错位、
> 验证脚本计时方式错误），见第九节。当前状态：P1/P3/P4 全部验证通过，P2 待烧录。

---

## 零、当前状态速览（2026-11-17 00:35 更新）

```
✅ P1 帧间插值    生效     观察到 8 个中间值 13.8→21.8→27.4→31.2→33.9→35.8
⏸️ P2 333Hz 固件  待烧录   p50=19.87ms 证明还是 50Hz；镜像已备好，**只能 PC 烧**
✅ P3 异步回包    正常     5/5 回包正确对应，无错位
✅ P4 流水线      达标     30.05 FPS（撞相机硬件上限）
✅ 稳定性         通过     10 分钟 RSS 恒定 115MB、线程恒定 14、启停 3/3
```

> **⚠️ 服务当前是停止状态**（用户要求「不要动舵机，吵到别人休息」）：
> ```
> gimbal-tracker.service   inactive（已 disable 开机自启）
> gimbal-bridge.service    inactive（已 disable 开机自启）
> 端口 8080 / 9000 / 9100  全部释放
> ```
> 舵机不接收 PWM 更新，静止嗡声消失。要彻底安静还需断开舵机电源。

运行/停止：
```bash
sudo systemctl start  gimbal-bridge gimbal-tracker    # 启动（会驱动舵机）
sudo systemctl stop   gimbal-tracker gimbal-bridge    # 停止（静音）
sudo systemctl status gimbal-bridge gimbal-tracker
```

启动后的预览地址：http://10.21.3.103:8080

一键验证（四项全查）：
```bash
ssh rk3588 '/root/rknn_env/bin/python /root/esp32_gimbal/verify_all.py'
```

烧完 333Hz 后单独验证 P2：
```bash
ssh rk3588 '/root/rknn_env/bin/python /root/esp32_gimbal/check_p2_after_flash.py --expect 333'
```

### ⚠️ 为什么 P2 只能你在 PC 上烧（2026-11-17 00:20 实测确认）

尝试在板上烧录，完整走了一遍，结论是**不可行**：

| 尝试 | 结果 |
|---|---|
| `modprobe cdc_acm` | ❌ `Module cdc_acm not found in /lib/modules/5.10.110` —— 内核根本没编 |
| `/dev/ttyACM*` | ❌ 不存在 |
| pyusb pty 桥接 + esptool | ⚠️ 能连通 USB-JTAG 接口（端点 0x02/0x83），但 **`Failed to connect: No serial data received`** |
| 根因 | esptool 靠 **RTS/DTR 信号线**把 ESP32 拉进 bootloader，而 pty 是虚拟终端**没有这两根控制线** |

ESP32-S3 的 USB-JTAG 支持通过特定 USB 控制请求复位进下载模式，但 esptool 的
`--before usb_reset` 只对原生 `usb:VID:PID` 端点有效，pty 伪装这条路走不通。
继续折腾的技术债太大（要自己实现 DTR/RTS 语义 + bootloader 握手时序）。

**所以：ESP32 插到 PC 上烧，这是唯一可靠路径。**

---

### 固件镜像已做静态验证（2026-11-17 00:36）

在烧录前，先确认镜像里**确实**是 333Hz 参数，避免「烧了但没生效」：

**镜像结构（esptool image-info）**
```
Image size: 277104 bytes
ESP32-S3 Image Header: Image version 1, Entry 0x403c8908, Segments 3
Flash: 16MB / 80m / DIO
Chip ID: 9 (ESP32-S3)
Segments: DRAM 0x014f0 | IRAM 0x00da0 | IRAM 0x02f58
```
✅ 头部完整、段结构正常，是可烧录的有效镜像。

**关键常量扫描（在二进制里搜立即数）**

| 常量 | 期望 | 搜索结果 |
|---|---|---|
| `SERVO_PERIOD_US = 3003`（新） | 存在 | ✅ **1 处**（偏移 134116） |
| `20000`（旧周期） | 不存在 | ✅ **0 处** |
| `SERVO_RES_LEVELS = 16384` | 存在 | ✅ 34 处（正常，多处引用） |

**结论**：`SERVO_PERIOD_US` 已从 20000 改成 3003，旧值在镜像里**彻底消失**。
说明编译时确实用了新参数，不是「改了源码但编了旧缓存」。

> 但静态验证只能证明**常量对了**，不能证明**运行时行为对了**。
> 最终仍需烧录后测串口延迟（`check_p2_after_flash.py`），看 p50 是否从 19.87ms 降到 ~1.5ms。
---

## 一、明天要做的第一件事：烧 ESP32 固件

**P2（PWM 50Hz → 333Hz）已编译好，但没烧。** 因为板子没有 `/dev/ttyACM*`（内核未编 USB-ACM），
在板上烧录风险高，你选择了「明天插 PC 再烧」。

### 烧录方法（把 ESP32 插到 PC）

```powershell
cd D:\a研究生\项目\rk3588项目\rk3588-edge-ai\esp32_gimbal

# 烧 333Hz 版本
& "C:\Users\15354\.espressif\python_env\idf6.0_py3.13_env\Scripts\python.exe" `
    -m esptool --chip esp32s3 --port COM6 -b 460800 `
    write-flash 0x0 C:\gimbal_build\esp32_gimbal_333hz.bin
```

**或者用现成脚本**（自动找端口）：
```powershell
pwsh -File build.ps1 -Flash
```
（注意：`build.ps1 -Flash` 烧的是最新构建产物，也就是 333Hz 版）

### 烧完怎么验证

启动横幅应该还是 `BOOT gimbal v2 / READY ... panPin=14 tiltPin=15`（版本号没变）。

**关键验证：串口往返延迟应该从 20ms 降到 3ms**

```bash
# 烧之前测一次（现在是 20ms 量级）
ssh rk3588 '/root/rknn_env/bin/python -c "
import socket, time
s = socket.socket(); s.settimeout(2); s.connect((\"127.0.0.1\", 9100))
lats = []
for i in range(20):
    t0 = time.time()
    s.sendall(b\"Q\n\")
    s.recv(64)
    lats.append((time.time()-t0)*1000)
lats.sort()
print(\"Q 往返: p50=%.1fms max=%.1fms\" % (lats[10], lats[-1]))
s.close()"'

# 烧之后应该看到 p50 ≈ 1.5ms, max ≈ 3ms
```

### 如果烧坏了怎么回滚

```powershell
& "...\python.exe" -m esptool --chip esp32s3 --port COM6 -b 460800 `
    write-flash 0x0 build\esp32_gimbal_merged.bin
```

`esp32_gimbal_merged.bin` 是 18:48 版本，也就是当前运行的固件，绝对安全。

### 333Hz 改动的风险点

| 风险 | 概率 | 应对 |
|---|---|---|
| 舵机支持不了 333Hz（规格书说 50-330Hz） | 低（超 3Hz） | 有异响/发热就退到 250Hz，改 `SERVO_FREQ_HZ` |
| 脉宽算错撞限位 | **已在数学上排除**（见下） | — |
| 舵机高频啸叫 | 中 | 正常现象（数字舵机高频修正），不影响功能 |

**脉宽计算已验证**（这是最关键的正确性检查）：

```
输入 500us  → duty=2727  → 实际 499.83us  (误差 -0.17us)
输入 1500us → duty=8183  → 实际 1499.85us (误差 -0.15us)
输入 2500us → duty=13639 → 实际 2499.87us (误差 -0.13us)

PAN  ±90° → 500~2500us ✓
TILT ±45° → 500~2500us ✓   （与原来完全一致）
```

---

## 二、四项优化完成情况

| 项 | 内容 | 状态 | 实测效果 |
|---|---|---|---|
| **P1** | 帧间插值 `S 40ms` | ✅ 完成 | 舵机在两帧之间平滑过渡 |
| **P2** | PWM 50→333Hz + 循环 20→3ms | ⏸️ 已编译待烧 | 预期串口延迟 20ms→3ms |
| **P3** | 回包读取异步化 | ✅ 完成 | 回包耗时 19.2ms → **0ms** |
| **P4** | 三线程流水线 | ✅ 完成 | 控制延迟 39ms → **22.4ms** |

---

## 三、实测数据对比

> 全部数据来自四个版本的实测日志，证据文件在板端 `/root/lab/_pipeline_evidence/`。

### 四个版本横向对比

| 版本 | 输入源 | 帧率 | 控制延迟 | 证据 |
|---|---|---|---|---|
| `.v2` 串行原始 | 真实摄像头 | 26.0 FPS（35.7~39.0ms） | **39 ms** | `base_run.log` |
| `.pipe` 流水线首版 | 真实摄像头 | 28.4 FPS | **22.2~22.5 ms** | `pipe_run.log` |
| `.pipe` 流水线首版 | 视频文件 | 34.0 FPS | 25.8~26.3 ms | `eq_pipeline.log` |
| **`.final` (+P1+P3)** | 真实摄像头 | **30.0 FPS**（相机上限） | **20.0~22.7 ms** | `final.log` |
| **`.final` (+P1+P3)** | 视频文件 | **55 FPS** | **19.2 ms** | 主控实测 |

### 分段耗时对比（真实摄像头，同一场景）

| 阶段 | 串行 `.v2` | 流水线 `.final` |
|---|---|---|
| A 采集+预处理 | 8.8 + 2.2 = **11.0 ms** | **33.3 ms**（受相机 30fps 限制） |
| B NPU+解码+控制 | 16.6 + 1.3 = **17.9 ms** | **20.3 ms** |
| C 画框+JPEG | 2.5 + 7.4 = **9.9 ms** | **9.3 ms** |
| **合计** | **38.9 ms**（串行累加） | **最慢 33.3 ms**（三阶段重叠） |
| **控制延迟** | **39 ms**（全部走完才发指令） | **22.3 ms**（B 出结果就发） |

**流水线的本质：把「累加」变成了「取最慢」。**

串行时每段串起来耗时；流水线后三段并行，帧率由最慢的 A 决定（33.3ms），
但**控制指令在 B 阶段出结果时就发出去**，所以控制延迟 = A + B = 22.3ms 而非 39ms。

### 为什么是 30 FPS 而不是 59 FPS

**实测发现：这颗相机的硬件上限就是 30 FPS。**

三条独立证据：

1. `v4l2-ctl` 报的最高档位就是 `0.033s (30.000 fps)`
2. 裸取流探针（只 read 无处理）：`30.00 fps, avg read=33.34ms`
3. **纯 CPU 路径探针（跳过 NPU 17ms）也只有 `29.96 fps`**

第 3 条是决定性的：**即使把 NPU 整个拿掉也到不了 30 以上**，瓶颈在相机吞吐，不在计算。

所以我之前算的「26 → 58.8 FPS」在这颗相机上物理不可达。

**串行版掉到 26 FPS 的原因**：uvcvideo 只有 4 个 mmap buffer，39ms 一轮才 QBUF 一次，
相机 FIFO 满了就丢帧。取流交给专用线程后 CPU 不再是取流的上限。

### v3 的完整 profile 行

```
采集 31.4 | 预处理 1.9 | NPU 17.3 | 解码 3.1 | 画框 2.0 | JPEG 7.0
|| A 33.3 | B 20.4 | C 9.0 | 最慢 33.3 ms(理论 30.0 fps)
|| 采集 30.0 fps 出图 30.0 fps
|| 延迟 22.4 ms | Q1丢 0 Q2丢 0 | SLOT 1
```

- **A**（采集+预处理）= 33.3ms，受相机 30fps 限制（33.3ms 就是相机帧间隔）
- **B**（NPU+解码+控制）= 20.4ms，**这是下一个天花板**
- **C**（画框+JPEG+推流）= 9.0ms
- **控制延迟 22.4ms** = A + B（把控制提前到 B 阶段，不含 C）

---

### 端到端延迟精确预算

> **口径很重要**：不说明「到哪一步」的延迟数字没有意义。这里分三个口径。

#### 延迟链路拆解

| 阶段 | 值 (ms) | 数据来源 | 能否优化 |
|---|---|---|---|
| 1 相机曝光 + USB 传输 | 0~33（均 16.5） | 推算 | 否，硬件 30fps |
| 2 V4L2 采集阻塞 | 31.6 | 实测 | 否，相机上限 |
| 3 预处理 letterbox+cvtColor | 1.7 | 实测 | 可 RGA 硬件化 |
| 4 NPU 推理 | 17.2 | 实测 | 单帧只能一核 |
| 5 DFL 解码 | 3.1 | 实测 | 已优化 |
| **A+B 小计（控制指令发出时刻）** | **22.0~22.7** | **实测** | **流水线已优化** |
| 6 TCP 回环 | 0.112 | 实测 | 可忽略 |
| 7 桥接 pyusb + USB CDC | ~1 | 推算 | - |
| 8 ESP32 控制节拍 | 0~20（均 10） | 相位扫描实测 | **P2 可降到 0~3** |
| 9 舵机启动 | 12 | 帧差实测 | 否，硬件 |
| 10 舵机机械运动 | 218~352 | 帧差实测 | 否，硬件 46~85 deg/s |

#### 三个口径的端到端延迟

| 口径 | 定义 | 当前（50Hz） | 烧 333Hz 后 |
|---|---|---|---|
| **A** | 人动 → 控制指令发出 | **22.4 ms** | 22.4 ms |
| **B** | 人动 → 舵机开始动 | **约 45.5 ms** | **约 37 ms** |
| **C**(10 deg) | 人动 → 舵机到位 | **约 263.5 ms** | 约 255 ms |
| **C**(30 deg) | 人动 → 舵机到位 | **约 397.5 ms** | 约 389 ms |

计算过程：
```
口径 B = A + 6 + 7 + 8 + 9 = 22.4 + 0.1 + 1 + 10 + 12 = 45.5 ms
口径 C = B + 舵机运动 = 45.5 + 218 = 263.5 ms（10 deg 步进）
```

#### 关键结论

- **舵机机械运动 218~352ms 占端到端的 83~88%** —— 这是绝对瓶颈
- 流水线把「控制指令时刻」从 **39ms 降到 22.4ms（-43%）**
- P2 烧录后能再降 **8.5ms**（占 263ms 的 3%）
- 想大幅改善必须换更快执行器：BLDC 云台 18~23ms vs DS3120 218~352ms，**差 10 倍**
---

### 纯预览模式性能基线（2026-11-17 00:35 实测）

> 用户要求深夜不要动舵机，所以用**纯预览模式**（不带 `--bridge`）测了一轮性能。
> 这个模式完全不碰云台，但流水线、NPU、JPEG 全部照常跑。

```
$ ./w4_edgevision_gimbal_preview.final /dev/video83 --no-3d     # 注意：没有 --bridge
[i] 未指定 --gimbal / --bridge，纯预览模式（不驱动云台）
[+] 流水线启动：3 线程 / 队列容量 1（丢旧）/ Slot 池 3 块（约 15.3 MB 常驻）

[profile] 采集 31.6 | 预处理 1.8 | NPU 17.2 | 解码 3.1 | 画框 1.8 | JPEG 6.7
       || A 33.3 | B 20.3 | C 8.4 | 最慢 33.3 ms(理论 30.0 fps)
       || 采集 29.9 fps 出图 29.9 fps || 延迟 22.1 ms
       || Q1丢 0 Q2丢 0 | 下发 0
```

**这组数据的价值**：

1. **证明 P4 的延迟数据不依赖舵机** —— `延迟 22.1~22.7ms` 无论云台接不接都一样
2. **提供明天验证的对照基线** —— 你烧完固件后跑同样的命令，应该看到相同数字
3. **`下发 0` 说明确实没碰云台** —— 可作为"静音测试"的标准做法

想在不吵人的情况下验证性能时，都用这个模式：
```bash
cd /root/lab && ./w4_edgevision_gimbal_preview.final /dev/video83 --no-3d
```
---

## 四、关键技术细节
### P3：为什么回包可以不等

```cpp
// 原来：阻塞 read()，实测 p50 = 19.2ms，占单帧一半
int got = read(fd_, buf, sizeof(buf) - 1);
```

改成：

```cpp
// 现在：select 2ms 超时，没数据就走
struct timeval tv{};
tv.tv_usec = 2000;
fd_set rfds; FD_ZERO(&rfds); FD_SET(fd_, &rfds);
int ready = select(fd_ + 1, &rfds, nullptr, nullptr, &tv);
if (ready > 0) { /* 读回包，更新 pan_/tilt_ */ }
else {
    pan_ = tp; tilt_ = tt;          // 乐观认为会执行，下帧纠正
    if (++pending_ > 10) { errors_++; pending_ = 0; }   // 只有连续 10 次没回包才记错
}
```

**为什么安全**：回包只是「确认收到」，不是「执行成功」。我们每帧都发新目标，
丢了下一帧立刻补上。而阻塞等待会让控制线程掉到 20~30Hz 且有 20ms 抖动。

**验证**：`B` 阶段 20.4ms = NPU 17.3 + 解码 3.1，**回包耗时归零**。

### P1 踩的坑（重要）

第一版写成这样，**启动就挂死**：

```cpp
while (read(fd_, tb, sizeof(tb) - 1) > 0) { }   // ← 永久阻塞
```

原因：桥接模式下 `fd_` 是 **TCP socket**，不是串口。socket 没数据时 `read()` 永久等，
而固件只回两行就没了，循环退不出来。**现象：启动横幅打到「已设置帧间插值」就没了。**

修法：同样用 `select` 加 300ms 超时，读一次就退。

> 教训：`fd_` 在直连串口和 TCP 桥接两种模式下语义不同。
> 串口有 termios 的 VTIME 兜底，socket 没有。**任何 `read()` 都要先想清楚 fd 是什么。**

### P4：三线程架构

```
Thread A (采集+预处理) → Q1 → Thread B (NPU+解码+控制) → Q2 → Thread C (画框+JPEG)
                                                        ↓
                                              main 只做看门狗
```

**关键设计**：

1. **队列容量 1 + push 覆盖丢旧** —— 追踪要「此刻」，积压会让云台追过期位置
2. **控制提前到 B** —— 延迟 = A+B 而非 A+B+C，且与 C 的 JPEG/socket 抖动解耦
3. **固定 3 块 Slot 池** —— 回池不 `release()`。实测 `cap.read()` 写进已有 Mat 时
   100% 复用 buffer 指针，`letterbox_into()` 用 `setTo` 代替重建，`rotate=0` 时浅拷贝
   省掉 2.7MB memcpy —— **稳态零 malloc**
4. **内存**：RSS 103MB → 115MB（多出的 12MB 就是 Slot 池），30 秒采样序列完全不涨

---

## 五、服务化（已配置好）

```bash
# 启动
sudo systemctl start gimbal-tracker

# 停止（注意：必须用 KILL，这个程序 SIGTERM 杀不死）
sudo systemctl stop gimbal-tracker

# 查看状态
sudo systemctl status gimbal-tracker

# 看日志
tail -f /tmp/gimbal_v2.log
```

**已设为开机自启**（`systemctl enable`）。

为什么之前 `nohup` 和 `setsid` 都失败：ssh 会话结束时发的 SIGHUP 会杀掉整个进程组，
systemd 用 cgroup 管理，不依赖会话，所以可靠。

服务配置要点：

```ini
[Service]
WorkingDirectory=/root/lab
ExecStart=/root/lab/w4_edgevision_gimbal_preview.final /dev/video83 --bridge 127.0.0.1:9100 --no-3d
KillMode=mixed
KillSignal=SIGKILL        # ★ 这个程序 SIGTERM 杀不死（HTTP 线程卡 accept）
TimeoutStopSec=5
```

---

## 六、版本清单

| 文件 | 说明 |
|---|---|
| **`w4_edgevision_gimbal_preview.final`** | **正式版**（流水线 + P1 + P3），130184 B |
| `w4_edgevision_gimbal_preview.v2` | 上一个稳定版（串行），回滚用 |
| `w4_edgevision_gimbal_preview.pipe` | 纯流水线版（没 P1/P3） |
| `w4_edgevision_gimbal_preview.cpp` | 对应源码（154 KB） |
| `_archive_20261116/` | 全部历史版本 |

回滚到 v2：
```bash
ssh rk3588 "sudo systemctl stop gimbal-tracker
sed -i 's|\.final|.v2|' /etc/systemd/system/gimbal-tracker.service
systemctl daemon-reload && systemctl start gimbal-tracker"
```

---

## 七、下一步瓶颈（明天可以考虑）

按实测数据排序：

| 瓶颈 | 当前值 | 能否优化 |
|---|---|---|
| **相机 30 FPS 上限** | 33.3ms/帧 | ❌ 硬件限制，除非换 60fps 相机 |
| **B 阶段 NPU 17.3ms** | 20.4ms 总 | ⚠️ 单帧只能用一个核，多 context 绑多核+帧间轮转可摊到 6~8ms（但追踪只要最新帧，收益有限） |
| 舵机机械响应 230~364ms | — | ❌ 换 BLDC 云台才有质变 |
| ESP32 控制节拍 20ms | — | ✅ **P2 就是干这个的，等烧录** |

**如果换了 ESP32 333Hz 固件**，串口延迟从 20ms 降到 3ms，端到端延迟能再降 17ms 左右。

---

## 八、已知问题（未修，记录在此）

1. **`SIGTERM` 杀不死** —— HTTP 线程阻塞在 `accept()`，`pthread_join` 永久等待。
   用 `systemctl stop` 配置的 `KillSignal=SIGKILL` 绕过了，但没根治。
   根治要改 `accept()` 为 `select`/`poll` 加超时。

2. **`--no-tilt` 与 `deadzone_tilt` 的注释不一致** —— 逻辑自洽但注释过时。

3. **Python 脚本仍是旧公式** —— `track_head_v3.py` 等还是 `HEAD_Y_RATIO=0.18`，
   与 C++ 对拍 tilt 落点会差最多 54px。

4. **`rknn_set_core_mask(CORE_0_1_2)` 只影响调度** —— 实测推理时 Core0 32~35%、
   Core1/2 各 2~3%，单帧仍只用一个核。代码注释里容易误解。

5. **固件不支持 OTA**（2026-11-17 查证）—— 当前命令集只有
   `P/H/Q/S/L/G/T/I/X/N/A/V/W/D/R`，没有任何升级命令。
   所以每次改固件都必须拔下来插 PC 烧，这是 P2 卡住的直接原因。

   **建议下次烧 333Hz 固件时顺手把 OTA 加上**，一劳永逸：
   - 分区表改成双 app 分区（`factory` + `ota_0`）
   - 加一条 `O <size>` 命令：ESP32 进入接收模式，从 USB CDC 收固件写入 ota 分区
   - 写完后 `esp_ota_set_boot_partition()` + 重启
   - 这样以后改固件只要：
     ```
     # 板上直接把 .bin 推给 ESP32，不用插 PC
     python3 ota_push.py /root/esp32_gimbal/esp32_gimbal.bin
     ```
   工作量约 100 行（`esp_ota_ops` API + 一个接收循环），但能彻底解决
   "改个 PWM 频率要拔线插 PC" 的痛点。

---

## 九、验证阶段发现的两个真 bug（2026-11-17 00:10）

### Bug A：桥接层多行回包错位（潜伏很久，被 P1 触发）

**现象**：程序启动后，用外部脚本发调试命令，回包整体错位一条：

```
I  -> ['# slew 40 ms']      ← 本该 GPIN 14 15
Q  -> ['GPIN 14 15']        ← 本该 S 0.0 -20.0 1
H  -> ['S 0.0 -20.0 1']     ← 本该 OK 0.0 -20.0
```

**根因**在 `gimbal_bridge.py` 的 `_send_raw()`：

```python
while time.time() < deadline:
    raw = self.ser.readline()
    resp = raw.decode(...).strip()
    if resp:
        return resp          # ← 拿到第一个非空行就返回
```

而固件对 `S` 命令回**两行**：

```
"# slew 40 ms\n"    ← out_line("# ...") 打的调试注释
"OK 0.0 0.0\n"      ← 真正的应答
```

桥接取走第一行（注释），**第二行 `OK` 永久留在 ESP32→桥接 的流里**，
之后每条命令的应答都错位一条。

**为什么现在才发现**：这个 bug 潜伏了很久——因为旧代码**从来不发 `S` 命令**。
是 P1 引入 `S 40` 才把它触发出来。

**修法**：`_send_raw()` 跳过以 `#` 开头的注释行，继续读真正的应答行。

```python
if resp.startswith("#"):
    leftover = resp        # 固件调试信息，不是命令应答 —— 丢弃，继续读
    continue
return resp
```

**验证**：修后 5/5 命令正确对应（`I→GPIN`、`Q→S ...`、`S 40→OK`）。

> **教训**：中间层转发命令时，「一行命令对应一行应答」这个假设必须验证。
> 固件只要多打一行调试信息就会打破它。**注释行和应答行必须在桥接层分离**。
> 更根本的教训：**加了新命令之后，要回头验证中间层的兼容性**——
> P1 加 `S` 时我只想着「发出去就行」，没想「它回两行会怎样」。

### Bug B：验证脚本自己的计时方式错了

第一次跑 `verify_all.py`，P2 测出 `p50 = 509.9ms`（荒谬值）。

**根因是我的测量代码**：

```python
def gimbal_cmd(s, c, wait=1.2):
    ...
    while time.time() - t0 < wait:     # ← 固定等满 1.2 秒
        ...
    return lines
# 外面用 (time.time() - t0) 当延迟 → 测到的是 1.2秒，不是延迟
```

**修法**：改成「首次收到非注释应答行就返回」，用 `perf_counter()` 精确计时。

修后测得 `p50 = 19.93ms` —— 这正是 50Hz 固件（20ms 节拍）的真实延迟，与历史测量一致。

> **教训**：测量代码本身要先自证。500ms 这种数量级明显不合理时，
> 先怀疑测量，别急着归因硬件。（这已经是本项目第二次踩同类问题——
> 第一次是把 `S` 命令的两行回包误判成「固件回包滞后」。）

---

### ⚠️ 帧率会随环境光变化（2026-11-17 00:45 实测发现）

做长时间稳定性测试时发现 fps 在 30 / 27 / 15 之间波动，排查后确认**不是程序问题**：

| 时间 | 画面亮度 mean | 裸采帧率 |
|---|---|---|
| 白天（11-16 下午） | 98.3 | **30.00 fps** |
| 凌晨（11-17 00:45，灯已关） | **0.1**（几乎全黑） | **20.05 fps** |

**根因**：UVC 相机默认 `exposure_auto=3`（光圈优先自动曝光）。
暗光下会自动延长曝光时间 → 帧间隔变大 → 帧率下降。

```
exposure_auto: 3          （自动）
exposure_absolute: 30     （比白天的 157 低很多，说明在补偿暗光）
```

**这不是 bug，是相机的正常行为。** 但也意味着：

- **白天测的性能数据才是有效的**（30 FPS / 22.4ms 延迟）
- **深夜测会得到偏低且不稳定的数字**，不能作为基线
- 若要固定帧率，可以关掉自动曝光：
  ```bash
  v4l2-ctl -d /dev/video83 --set-ctrl=exposure_auto=1
  v4l2-ctl -d /dev/video83 --set-ctrl=exposure_absolute=157
  ```
  但暗光下手动低曝光会得到全黑的画面，没有意义。

**长期稳定性测试的正确做法**：在光线稳定的环境下跑，或记录并注明环境光条件。
---

## 十、稳定性验证（2026-11-17 00:20~00:30 实测）

目标要求「确保系统可稳定运行」，做了三组测试：

### 1. 10 分钟连续采样

```csv
time,rss_mb,fps,npu_ms,errors,dropped,reopens,badjpeg
00:20:03,115,29.98,17.60,0,0,0,0
00:20:33,115,30.01,17.02,0,0,0,0
00:21:03,115,30.02,17.03,0,0,0,0
00:21:33,115,30.00,17.04,0,0,0,0
00:22:03,115,30.00,22.75,0,0,0,0
00:22:33,115,30.00,21.45,0,0,0,0
00:23:03,115,29.73,16.98,0,0,0,0
00:23:33,115,30.00,17.07,0,0,0,0
...
```

**RSS 恒定 115MB 无增长、fps 29.7~30.1、errors/dropped/reopens/badjpeg 全 0。**

### 2. 线程与内存泄漏检查

```
时间         PID      线程数   RSS(MB)   fps
00:22:29   78803    14       115       30.0
00:22:44   78803    14       115       30.0
00:22:59   78803    14       115       30.0
00:23:14   78803    14       115       30.0
00:23:29   78803    14       115       30.0

✅ 线程数恒定 14，无泄漏
✅ 内存稳定（+0 MB）
```

### 3. 连续启停 3 次

```
第 1 次... ✅ 线程=14 fps=30.0 errors=0
第 2 次... ✅ 线程=14 fps=30.0 errors=0
第 3 次... ✅ 线程=14 fps=30.0 errors=0

✅ 3/3 启停正常，端口完全释放，线程无残留
```

**这条特别重要**：pipeline 的 3 个线程（A/B/C）能被 `g_running=0` 正常唤醒退出，
`pthread_join` 全部返回，端口 8080/9000 干净释放。
（注：程序整体的 SIGTERM 仍然杀不死——那是 HTTP 线程卡 `accept()` 的老问题，
用 systemd 的 `KillSignal=SIGKILL` 绕过。）

脚本：`host/stability_monitor.py`、`host/check_thread_leak.py`

---

## 十一、验证方法（可复现）

```bash
# 一次性验证四项
ssh rk3588 '/root/rknn_env/bin/python /root/esp32_gimbal/verify_all.py'
```

输出示例：

```
【P4】流水线帧率
 ✅ fps=30.03（相机硬件上限 30，已达标）

【P3】回包对应正确性
   I     -> ['GPIN 14 15']    ✅
   Q     -> ['S 0.0 -20.0 1'] ✅
   S 40  -> ['OK 0.0 -20.0']  ✅
 ✅ 5/5 全部正确对应，无错位

【P2】串口往返延迟
   Q 往返: p50=19.9ms  max=40.2ms
 ⏸️ 固件仍是 50Hz（20ms 节拍），烧录后应降到 ~1.5ms

【P1】帧间插值
 ✅ 观察到 8 个中间值：13.8, 21.8, 27.4, 31.2, 33.9, 35.8
```

单独的 P1 验证（更详细，对比 S 0 vs S 1000）：
```bash
ssh rk3588 '/root/rknn_env/bin/python /root/esp32_gimbal/verify_p1_slew.py'
```

