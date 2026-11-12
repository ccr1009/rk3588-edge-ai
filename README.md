# EdgeVision RK3588

> 一块 RK3588 开发板上的两个工程方向：
> **把 YOLOv8 在 NPU 上压到 20.58 ms**，以及**让 2B 大模型在板子上离线跑通"感知-决策-执行"闭环**。
>
> 所有数字都来自真实链路，能当场复现。未实现的功能明确列在[「明确未实现」](#明确未实现不要对外宣传)表里。

[![Platform](https://img.shields.io/badge/Platform-Rockchip%20RK3588%20(6.0%20TOPS)-blue.svg)]()
[![C++](https://img.shields.io/badge/Language-C%2B%2B17%20%7C%20Python%203.10-orange.svg)]()
[![RKNN](https://img.shields.io/badge/NPU%20Toolkit-RKNN--Toolkit2%20v2.3.0-green.svg)]()
[![RKLLM](https://img.shields.io/badge/On--Device%20LLM-RKLLM%20%2B%20Qwen3.5--2B-purple.svg)]()
[![Pipeline](https://img.shields.io/badge/Pipeline-MPP%20硬解%20%2B%20DFL--Free%20%2B%20三核NPU-red.svg)]()

---

## 📖 这份 README 怎么用

**我只想看/用，不改代码** → 跳到 [五分钟上手](#五分钟上手)
**我要改代码** → 先读 [`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md)（每个目录是什么、该不该动）
**我要准备面试** → 跳到 [面试素材](#面试素材)
**我想知道现在能干什么** → 读 [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md)

---

## 实测演示

![EdgeVision RK3588 检测与 BEV 演示](docs/images/bev_demo_snapshot.png)
*单帧真实推理输出：NPU 检测框 + 单目 3D 线框 + 自车坐标系 BEV 鸟瞰栅格 + 顶部吞吐/NPU 调度 HUD。
来自**板端 C++ 链路**（MPP 硬解 → NPU → 后处理 → BEV 投影），不是仿真渲染。*

### 三个可实时访问的入口

| 入口 | 端口 | 画面内容 | 分辨率/帧率 | 占用摄像头 |
| :--- | :---: | :--- | :--- | :---: |
| **车辆检测看板**（推荐演示） | **8090** | 真实路景 + 每车彩色 3D 立方体框 + 距离标注 + BEV 雷达 | 1280×720 @ **24~28 fps** | ❌ 读 mp4 |
| **摄像头实时预览** | **8080** | 摄像头画面 + 3D 框 + BEV 雷达 + 遥测 HUD | 1920×1080 @ **~21 fps** | ✅ |
| **车载 3D 看板**（摄像头模式） | 8090 | 同看板，输入换成摄像头 | 1280×720 @ **~29 fps** | ✅ |

> **8080 和 8090 功能几乎一样**（同一套视觉算法、都有 3D 框和 BEV 雷达），区别是：
> 8080 分辨率高（1080p）但没有数据接口；8090 有 `/api/stats` JSON（每辆车的类别/置信度/距离）。
> **面试演示用 8090**，能拿出结构化数据比只有截图有说服力。

![8090 看板实时画面](docs/images/bev_dashboard_live_720p.jpg)
*8090 看板：`7.4m / 12.4m / 15.8m / 19.0m / 25.5m` 距离标注 + BEV 雷达 5 个目标 + `3D 5/5`*

---

## 五分钟上手

### 前提

- RK3588 开发板（本项目用 **EVM3588-B**，8 GB + 64 GB eMMC，Debian 11 / 内核 5.10.110）
- 有线网连上板子，能 SSH
- 板子 IP 不入库，用环境变量传：

```bash
export EDGEVISION_BOARD_HOST=<board-ip>     # Windows: set EDGEVISION_BOARD_HOST=<board-ip>
```

### 路径 A：只想看画面（3 条命令）

```bash
# ① 启动车辆检测看板（读 mp4，不抢摄像头）
ssh root@<board-ip> 'systemctl start edgevision-dashboard'

# ② 浏览器打开
#    http://<board-ip>:8090
```

就这样。**8090 是最稳的演示入口**：不依赖摄像头，不会和其他服务冲突。

### 路径 B：看真实摄像头

```bash
ssh root@<board-ip> 'systemctl start edgevision-preview'
# 浏览器 http://<board-ip>:8080
```

⚠️ **8080 独占摄像头**。如果 9000 网关或 8090 摄像头模式在跑，会打不开。
先停掉它们：

```bash
ssh root@<board-ip> 'systemctl stop edgevision edgevision-dashboard-cam'
```

### 路径 C：语音控制（推荐，最能体现"具身智能"）

```bash
ssh root@<board-ip> 'systemctl start voice-gateway'
# 等约 70 秒就绪，然后对麦克风说「小瑞，打开车辆检测看板」
```

Agent 会调 `control_service` 启动服务，并汇报完整地址。**实测走快速通道，0.1 秒响应。**

能说的话见 [Agent 能控什么](#agent-能控什么)。

### 路径 D：跑 TCP 推流（给 PC 程序用）

```bash
# 板子
ssh root@<board-ip> 'systemctl start edgevision'

# PC（需要 EDGEVISION_BOARD_HOST）
python lab/tests/pc_client_viewer.py
```

逐帧打印 `帧号 | FPS | 类别: 置信度, 框坐标`，实测 **19~30 FPS**。

---

## 系统架构（**已实现**的链路）

> 本图只画跑通并有实测数据的模块。规划中但**尚未实现**的部分单独列在
> [「明确未实现」](#明确未实现不要对外宣传)一节，不混进架构图里。

### A. 视觉推理数据面（C++ 原生，板端）

```
        [ USB 摄像头 / 静态 JPEG ]
                      │
                      ▼
    ┌──────────────────────────────────────────────┐
    │  MPP 硬件 JPEG 解码 (VPU)                     │
    │  - MJPEG → NV12，直出物理连续内存             │
    │  - 实测 p50 1.03 ms，软解 7.76 ms → 7.6×      │
    │  - 9/9 图 PSNR 49.9~77.2 dB 验证一致          │
    └──────────────────┬───────────────────────────┘
                       ▼
    ┌──────────────────────────────────────────────┐
    │  NV12 → RGB888 CSC + letterbox 640×640        │
    │  - ⚠️ 当前是 CPU 定点实现（O3 后 33.58 ms）   │
    │  - RGA 硬件化是**待做项**，未实现             │
    └──────────────────┬───────────────────────────┘
                       ▼
    ┌──────────────────────────────────────────────┐
    │  RK3588 三核 NPU  (6.0 TOPS INT8)            │
    │  - rknn_set_core_mask 硬绑定 Core0/1/2        │
    │  - 3 context 真并行：59.3 FPS / 2.21×         │
    │  - 纯推理 15.95 ms（原 84ch 模型 37.19 ms）   │
    └──────────────────┬───────────────────────────┘
                       ▼
    ┌──────────────────────────────────────────────┐
    │  DFL-Free 后处理（自己做的图手术）            │
    │  - 模型里砍掉 DFL 头，解码搬到 CPU            │
    │  - 解码 + NMS：O0 3.44 ms → O3 0.62 ms        │
    └──────────────────┬───────────────────────────┘
                       ▼
        三个出口：8080 MJPEG / 9000 TCP 定长帧 / 8090 HTTP
```

### B. 端侧 Agent 决策面（纯单板离线）

```
    用户语音 ──▶ ASR (Paraformer, RTF 0.023)
                    │
                    ▼
             软唤醒「小瑞」
                    │
                    ▼
    ┌───────────────────────────────────────┐
    │  Qwen3.5-2B W8A8 (RKLLM, 3 核 NPU)    │
    │  ReAct: 思考 → 选工具 → 执行 → 回填   │
    └───────────────┬───────────────────────┘
                    │ Tool Calling
        ┌───────────┼───────────┬──────────────┐
        ▼           ▼           ▼              ▼
   温度/NPU     LED 控制    视觉遥测      服务启停(6个)
   /sys/class  /sys/class  TCP :9000     systemctl
```

### C. 资源争用（本项目最真实的工程约束）

| 资源 | 谁抢 | 后果 | 解法 |
| :--- | :--- | :--- | :--- |
| **NPU IOMMU 4 GiB** | Agent(2 GB) + 视觉(~200 MB) | 第 3 个 `errno 14` | 起 Agent 前清场 |
| **摄像头** | 8080 / 8090-cam / 9000 | 后者静默假死（能 open 但没帧） | Agent 层 `fuser` 探测 |
| **端口 8090** | mp4 模式 / 摄像头模式 | 同端口冲突 | `Conflicts=` + Agent 拦截 |
| **麦克风** | ASR / KWS / 录音 | 后来者 `exit 255` | unit 里 `fuser -k -9` |

---

## 常用命令速查

### 服务管理

```bash
# 查看全部状态
systemctl status edgevision edgevision-preview edgevision-dashboard \
                 edgevision-dashboard-cam edgevision-patrol voice-gateway

# 启动/停止（把 <unit> 换成上面任一）
systemctl start  <unit>
systemctl stop   <unit>
systemctl restart <unit>

# 看日志
journalctl -u <unit> -f
```

### 端口对照

| 端口 | 用途 | systemd unit | 占摄像头 |
| :---: | :--- | :--- | :---: |
| **8080** | 摄像头实时预览（1080p） | `edgevision-preview` | ✅ |
| **8090** | 车辆检测看板（mp4） | `edgevision-dashboard` | ❌ |
| **8090** | 车辆检测看板（摄像头） | `edgevision-dashboard-cam` | ✅ |
| **9000** | TCP 定长帧推流（248 B） | `edgevision` | ✅ |
| **8088** | Agent RPC 控制面 | — | ❌ |
| **8081** | 双目 stereo3d | — | ✅ |

> ⚠️ **同一时刻只能有一个服务占用摄像头**。8xxx 和 9xxx 之间要错开。
> 8090 的两种模式同端口，也不能同时开。

### 摄像头坏了怎么查

```bash
# ① 谁占着？（这是最快的一步）
fuser -v /dev/video83

# ② 它真的能出帧吗？（0 字节 = 硬件问题，不是代码问题）
v4l2-ctl -d /dev/video83 --stream-mmap=4 --stream-count=10 --stream-to=/tmp/t.out
ls -l /tmp/t.out

# ③ 换设备对照（这条最省时间）
v4l2-ctl -d /dev/video81 --stream-mmap=4 --stream-count=10 --stream-to=/tmp/t2.out
```

### NPU 内存不够（`errno 14`）

```bash
bash /root/npu_doctor.sh    # 六节体检（注意用 bash 调用，该文件没有 +x 权限）
dmesg | grep -i 'used:' | tail -3
```

**根因**：RK3588 的 NPU IOMMU 只有 **4 GiB 地址空间**（与物理内存 7.5 GB 无关）。
2B 模型每实例约吃 1.95 GB。**起 Agent 前必须停掉视觉服务。**

仓库副本：`tools/npu_doctor.sh`

---

## Agent 能控什么

Agent 通过 `control_service` 工具管理 **7 个服务别名 / 6 个 systemd unit**，支持 `start` / `stop` / `status`。

| 你说 | Agent 做什么 | 占摄像头 | unit |
| :--- | :--- | :---: | :--- |
| "打开车辆检测看板" | 启动 8090（mp4 模式） | ❌ | `edgevision-dashboard` |
| "用摄像头跑车辆检测" | 启动 8090（摄像头模式） | ✅ | `edgevision-dashboard-cam` |
| "打开摄像头预览" | 启动 8080 | ✅ | `edgevision-preview` |
| "打开推流网关" | 启动 9000 | ✅ | `edgevision` |
| "打开巡检守护" | 启动快层守护 | 只读探测 | `edgevision-patrol` |
| "关掉看板" / "关掉预览" | 停对应服务 | — | 同上 |
| "看板在跑吗" | 查状态 | — | 同上 |
| **"启动舵机跟随"** | **答"舵机还没接入"** | 硬件缺失 | 无 unit |

### 三个已实现的保护

1. **摄像头冲突检测**：启动前用 `fuser` 探真实占用，返回 `error: camera_busy` + 谁占着，
   **绝不静默停掉另一个服务**（7×24 推流断掉你看不见）
2. **端口冲突检测**：8090 两种模式互斥，返回 `error: port_busy`
3. **舵机防误判**：快速通道直接答死，不会去猜成其他服务
   （曾出现过说"舵机"导致 Agent 把自己关掉的 bug）

### 服务启动后一定报地址

工具返回的 `url` 字段是**实测出来的板子 IP**（`hostname -I`），不是模型编的。
prompt 明确禁止说 `localhost`——你在自己电脑上开 localhost 指的是你的机器。

---

## 板端实测基准（EVM3588-B，全部来自真实链路）

> 口径说明：以下均为 **RK3588 物理开发板**实测，不是仿真、不是理论值、不是"预估"。
> 每条的方法、样本量、局限见 [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md)；
> 不同口径（纯推理 / 全周期 / 全链路）已分开列出，不混在一张表里。

### 推理与后处理

| 指标 | 优化前 | 优化后 | 提升 | 口径 |
|---|---:|---:|---:|---|
| 纯推理 `rknn_run` | 37.19 ms | **15.95 ms** | −57.1% | 200 次，单核 |
| DFL 解码 + NMS | 3.09 ms | **0.62 ms**（O3） | 5.5× | O0/O3 对照，`taskset -c 6` |
| 单帧全链路 | 37.19 ms / 26.9 FPS | **20.58 ms / 48.6 FPS** | −44.7% / +81% | 纯推理+IO+后处理 |
| 三核并行聚合 | — | **59.3 FPS（2.21×）** | — | 3 个独立 context 绑 3 核 |

### 图像处理（生产链路 `edgevision_pro_main`，O0 vs O3）

| 阶段 | O0 | O3 | 变化 | 说明 |
|---|---:|---:|---:|---|
| MPP 硬解 | 1.250 ms | 1.322 ms | +5.8% | **对照组①**：硬件层不该变 ✔ |
| **CPU CSC** | **52.016 ms** | **33.583 ms** | **1.549×** | 定点 BT.601，最大瓶颈 |
| letterbox | 1.184 ms | 1.222 ms | +3.2% | **对照组②**：OpenCV 预编译库动不了 ✔ |
| NPU + DFL | 22.480 ms | 19.708 ms | −2.77 ms | 与 bench 口径吻合 |
| **全链路** | **76.928 ms** | **55.836 ms** | **−27.4%** | 13.0 → 17.9 FPS |

### 其余模块

| 指标 | 数值 | 口径 / 局限 |
|---|---|---|
| **8090 看板（mp4）** | **24~27 fps**（p50 25.71），单帧 p50 **38.90 ms**，1280×720 MJPEG 144 KB/帧 | 200 样本直采 `/api/stats`；**带背景负载测的**，输入是视频文件 |
| **8090 看板（摄像头）** | **29.19 fps / 34.26 ms** | 实测 `/api/stats`，比 mp4 模式快（省掉 H.264 软解 5.3 ms） |
| **8080 预览（摄像头，1080p）** | **~21.7 fps / 44.6 ms** | 分辨率更高，所以单帧更慢 |
| 看板分段耗时 | `capture 5.42 + prep 7.84 + npu 16.39 + draw 1.58 + encode 7.55 = 38.90 ms` | **瓶颈在 CPU 侧（prep+encode 40%）不在 NPU（42%）** |
| MPP 硬解 MJPG | **p50 1.03 ms** vs 软解 7.76 ms（7.6×） | 200/200 有效帧，9 图 PSNR 49.9~77.2 dB |
| INT8 量化掉点 | **mAP@0.5 0.6087 → 0.6002（−0.85 点 / −1.4%）** | COCO128 是训练子集，绝对 mAP 偏乐观，相对掉点成立 |
| 模型体积 | INT8 **6.22 → 4.34 MB（−30.2%）** | FP32 ONNX 12.2 MB，两步分开算 |
| 端侧 Agent 端到端 | **106.5~112.2 s** / 4~5 个工具 / **1315~1350 字** | Qwen3.5-2B W8A8，单板离线，三次独立测量 |
| ASR | RTF **0.023**（比实时快约 40×） | Paraformer-zh-small int8 |
| TTS | RTF **0.564**（4 线程最优） | vits-zh-fanchen-C |
| 巡检守护告警闪灯 | 占空比 **49.7%** vs 基线 20.0%（+29.7pp） | 0.02 s 采样，6 s / 300 点 |
| Agent 回归测试 | **12/12 通过** | 含故障注入，两次独立全量 |

### 明确未实现（不要对外宣传）

| 项目 | 状态 |
|---|---|
| RGA 硬件 CSC / 缩放 | **未实现**，当前是 CPU 定点实现 |
| DMA-BUF / 全链路零拷贝 | **未实现**，`rknn_inputs_set` 走普通内存拷贝 |
| RTSP / WebRTC 推流 | **未实现**，实际是 TCP 9000 自定义 248 B 定长帧协议 |
| H.264 / H.265 硬解 | **未实现**，MPP 只做了 MJPEG；看板 mp4 输入是 **libavcodec 软解** |
| 看板跑满 30 fps | **未实现**，实测 24~28 fps（源视频 30 fps 的 82~90%） |
| 目标跟踪（跨帧 ID 稳定） | **未实现**，每帧独立检测，`#idx` 跨帧**不连续** |
| ByteTrack 进 C++ | **未实现**，`track/` 下只有 Python 仿真 |
| 整机功耗 | **未测**，无功率计 |
| MPP 硬编 JPEG | **已实现但已否掉**，实测比软编慢（见关键技术点 5） |
| **舵机跟随** | **代码有、硬件未接**。等 ESP32-S3 到货 |

---

## 关键技术点（面试用）

<details>
<summary><b>1. 官方 5.5ms vs 实测 37ms：6 倍差距，不靠猜归因</b></summary>

逐层剖析发现 **49%（18.44 ms）耗在 CPU 回退的 DFL 检测头**（Transpose 9.2 + Softmax 4.5 + Reshape 4.4 ms），
NPU 主体只占 19.23 ms 且三核并行时每核仅 49% 负载。

→ 结论：**先改模型导出（砍 DFL 头），别一上来就做零拷贝**（实测喂数据方式对延迟无影响）。

</details>

<details>
<summary><b>2. 余弦相似度 0.9965 骗了我：cls 分支塌缩成常数</b></summary>

旧校准集是**合成色块动画**抽帧，与真实场景分布严重偏离。
两版模型 9 图 top-1 平均分：新真实校准集 0.8072 vs 旧合成校准集 **0.5000**。

关键证据：**旧组分数全部精确等于 0.5000 = sigmoid(0)** → cls 分支整体塌缩成常数、分类头失效。
框还对是因为 box 分支没坏。

→ **教训：特征图整体相似（余弦 0.9965）不影响单个分支塌缩，真精度必须跑 mAP。**

</details>

<details>
<summary><b>3. 演示全绿但 Agent 在说谎：分层定位"幻觉 vs 感知层喂错数据"</b></summary>

演示判 PASS，但翻 trace 发现 Agent 汇报"LED 熄灭导致过热保护被抑制"，而灯**正在爆闪**。

根因不是模型幻觉：`get_led_current_mode()` 单点读 `trigger` 撞上 100 ms 爆闪的灭相。
**周期性物理量用瞬时单点采样定性 = 混叠误判。**

→ 修法：查爆闪 pid + 兜底 12×15ms 多点采样；`control_led` 调用 **4 次 → 1 次**。

**方法论**：Agent 给出离谱结论时，先分层定位「模型幻觉 vs 感知层喂错数据」，再谈调 prompt。

</details>

<details>
<summary><b>4. 编译优化白捡 1.55×：build type 为空跑了一个月 -O0</b></summary>

`CMakeCache.txt` 里 `CMAKE_BUILD_TYPE` **是空的**，`flags.make` 里 `CXX_FLAGS = -std=gnu++17` ——
**一个 `-O` 都没有**。此前所有 CPU 侧数字全是 -O0 的悲观值。

A/B 实测（`taskset -c 6`，6 轮交替）：
- 纯推理 14.79~14.95 ms **无变化**（对照组：证明测量没漂）
- **DFL 解码+NMS 3.44 → 0.62 ms = 5.5×**
- **全链路 76.928 → 55.836 ms = −27.4%**

→ **方法论：不绑核时 O0 的 p50 仍是 3.45 ms 但 mean 被拉到 6.4~8.2 ms** ——
调度迁移的长尾污染均值。性能测量必须 `taskset`。

</details>

<details>
<summary><b>5. MPP 硬编走通了，但我把它从路线图划掉</b></summary>

实现了并验证正确（NV12 → baseline JPEG，9 图 PSNR 42~49 dB），但 **1280×720 mean 7.24 vs 软编 6.65 ms（反而慢 1.07~1.09×）**，
**分辨率 64×64~960×540 全部档位都更慢**。

根因（延迟模型）：硬编 = **0.35 ms 固定开销 + 7.0e-6×像素**，软编 = 0 + 7.5e-6×像素
→ 每像素算力打平，硬编白付 ioctl+中断往返。

唯一优势：CPU 占用 8.2% vs 99.7%，但单路 30 fps 场景 CPU 不是瓶颈。

→ **负结论也是结论。用数据把路线图上的假设否掉，比硬凑一个"快了 5%"有价值。**

</details>

---

## 代码在哪、怎么编译

> **完整目录说明见 [`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md)**（每个目录是什么、该不该动）

### 三条主线

| 目录 | 内容 |
| :--- | :--- |
| [`lab/`](lab/) | **项目主线源码**（agent / vision / voice / services / tests） |
| [`deploy/cpp/`](deploy/cpp/) | 视觉引擎 CMake 工程（6 个 target） |
| [`docs/`](docs/) | 现状 / 基准 / 架构 / 踩坑 |

### 编译（**一律在板子上原生编译**）

交叉编译会撞 glibc：Ubuntu 22 的 2.35 vs 板子 Debian 11 的 2.31。

```bash
# CMake 工程（视觉引擎）
cd /root/edgevision/deploy/cpp
cmake -S . -B build_release -DCMAKE_BUILD_TYPE=Release -DCROSS_COMPILE_RK3588=ON
cmake --build build_release -j4

# Agent（一条命令：同步 → 板上编译 → 可选重启）
bash lab/build/deploy.sh agent

# 其余程序
bash lab/build/build_w4_gateway.sh      # 9000 网关
bash lab/build/build_preview.sh         # 8080 预览
bash lab/build/build_patrol_daemon.sh   # 巡检守护
```

> ⚠️ **CMake 曾因 build type 为空跑了一个月 `-O0`**（关键技术点 4）。
> 现在 CMakeLists 已加自检：缺 `-O` 会 `message(WARNING)`。

### 改完代码必须跑回归

```bash
python3 /root/regress.py    # 20 条，约 2 分钟
```

---

## 文档地图

| 文档 | 内容 | 什么时候看 |
|---|---|---|
| [`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md) | **每个目录是什么、该不该动** | **改代码前必读** |
| [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md) | 现在能干什么 + 下一步 | 接手先看这份 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 三条技术线怎么串、端口进程表 | 讲项目从这里开始 |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | 全部性能/精度实测，含口径与局限 | 想知道数字怎么来的 |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | **55 条**踩坑记录 | 遇到问题时查 |
| [`docs/OPEN_ISSUES.md`](docs/OPEN_ISSUES.md) | 未关闭问题清单（A/B/C 分类） | 改代码前看，别重复已有取舍 |
| [`AGENTS.md`](AGENTS.md) | 项目总纲与工作约定 | 了解这个仓库怎么协作 |

### 专项技术笔记（`docs/tech_notes/`）

| 文档 | 内容 |
| :--- | :--- |
| `AGENT_TOOLS.md` | Agent 7 个硬件工具的契约与设计取舍 |
| `AGENT_MULTISTEP_DEMO.md` | 两幕演示设计 + 3 个缺陷复盘 |
| `AGENT_TEST_PLAN.md` | 12 用例表 + 指标口径 + 故障注入 |
| `PATROL_DAEMON_DESIGN.md` | 快慢双系统，为什么快层不用 LLM |
| `VOICE_ASR_PLAN.md` | 语音链路（ASR/TTS/唤醒/编排） |
| `BEV_GATEWAY_ARCHITECTURE.md` | 视觉网关与 BEV 投影 |
| `CAMERA_FPS_ROOT_CAUSE.md` | 摄像头帧率根因分析 |

---

## 面试素材

### 五条杀手故事（各 90 秒，详见 `docs/interview_book/src/14-项目难点与易被追问的点.md`）

1. 官方 5.5 ms vs 实测 37 ms，同算子位置不同差 6 倍，归因不靠猜
2. 余弦 0.9965 骗了我，cls 精确等于 0.5 = sigmoid(0)
3. 演示全绿但 Agent 在说谎 → 分层定位「模型幻觉 vs 感知层喂错数据」
4. 模型不缺智商缺常识（49°C 判"永久损坏"）
5. MPP 硬编走通了但我把它从路线图划掉

### 绝对不能说出口的（红线）

> 端到端 18 ms / 三核 150+ FPS / 功耗 9.5 W / DMA-BUF 零拷贝 / RGA 硬件 CSC /
> RTSP-H.264 硬解 / 4 路 1080P / ByteTrack 进 C++ / 余弦 0.9965 当精度

### 面试材料清单

[`docs/interview_book/`](docs/interview_book/) —— **18 篇深度问答**，按主题分：

| 篇 | 内容 |
| :--- | :--- |
| `00-怎么用这本书` | 阅读顺序与用法 |
| `01-项目总览与技术选型` | 两分钟自述素材 |
| `02~05` | 文件IO/进程、IPC与信号、多线程、epoll与Socket网关 |
| `06~09` | RK3588与NPU、视觉流水线、性能优化方法论、INT8量化 |
| `10~12` | RKLLM端侧大模型、ReAct Agent设计、巡检守护与可靠性 |
| `13~17` | 调试排障方法论、难点与追问、压力面、两个求职方向 |

另：[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) 是所有数字的**唯一可信来源**。

---

## 已知限制（诚实说明）

**服务层**
- 8080/8090/9000 三者都要独占摄像头，不能同时开
- Agent 与视觉服务共享 4 GiB NPU IOMMU，起 Agent 前必须清场
- 语音网关 + 视觉服务同跑时，Agent 响应从 10 s 劣化到 39 s

**Agent 层**
- 唤醒严格命中 **80%**（8/10），误识集中在「角瑞」「巧瑞」这类 X瑞 族
- decode **10 tok/s**，看板在跑时降到 3 tok/s（内存带宽锁死）
- 无 barge-in（播报期间不听）
- 上下文 16K 无主动管理

**硬件**
- **cam1（`/dev/video81`）当前已物理移除**，板上只有 cam2
- 舵机云台硬件未到货

**工程债**（见 [`docs/OPEN_ISSUES.md`](docs/OPEN_ISSUES.md)）
- `cam_read()` 等公共逻辑在 4 个文件里重复实现
- 除 `deploy/cpp` 外的程序都靠手写 `g++` 命令编译
- 端口号硬编码分散在 10~15 处

---

## 相关链接

- **协议规格**：248 字节定长帧（8 B header + 10×24 B BBox），见 `lab/vision/edgevision_gateway.cpp` 头部注释
- **客户端**：[`lab/tests/pc_client_viewer.py`](lab/tests/pc_client_viewer.py)
- **验收脚本**：[`scripts/verify_bev_dashboard.py`](scripts/verify_bev_dashboard.py)（6 项检查，纯标准库）

---

<p align="center">
  <i>本项目遵循一条原则：不许用假数据冒充结果。</i><br>
  <i>所有数字来自真实链路，未实现的功能明确列出。</i>
</p>
