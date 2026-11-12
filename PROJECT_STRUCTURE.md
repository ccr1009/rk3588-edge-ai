# 项目结构总览（PROJECT STRUCTURE）

> **这份文件回答一个问题：这个目录里每个东西是什么、我该不该动它。**
>
> 读代码前先看这里，能省掉 80% 的摸索时间。
> 配套：[`README.md`](README.md)（怎么用）、[`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md)（现在能干什么）
>
> 最后核对：2026-11-12（结构变动请同步更新本文件）

---

## 一、30 秒看懂

```
edgevision-rk3588/
├── README.md              ← 怎么用（快速上手）
├── PROJECT_STRUCTURE.md   ← 本文件：每个目录是什么
├── AGENTS.md              ← 协作须知（AI/新人接手先读）
│
├── lab/                   ★ 项目主线源码（按职责分 8 个子目录）
├── deploy/cpp/            ★ 视觉引擎 CMake 工程（6 个 target）
├── docs/                  ★ 文档（现状/基准/架构/踩坑）
│
├── esp32_gimbal/          独立工程：ESP32 云台执行器
├── stereo3d/              独立工程：双目 3D
├── convert/               模型转换与量化（PC + 板端）
├── data/                  数据集与模型（104 MB，大文件在这）
│
└── 【历史遗留，别动】demo/ gateway/ bev/ pipeline/ track/ simulate/ outputs/
```

**三个"★"是你要动的。其余要么是独立工程，要么是早期原型。**

---

## 二、主线代码在哪

### `lab/` — 项目主线（178 文件，8 个子目录）

这是**唯一需要日常修改的地方**。按职责划分：

| 子目录 | 放什么 | 是否主线 | 说明 |
| :--- | :--- | :---: | :--- |
| `agent/` | Agent 主体、巡检守护、8088 控制面 | ★ **是** | 项目最核心，见下方详解 |
| `vision/` | **9000 网关**、8080 预览 | ★ **是** | 9000 网关是脊柱 |
| `voice/` | 语音网关、TTS 常驻、启停脚本 | ★ **是** | |
| `services/` | systemd unit（**6 个 .service + 4 个配置文件**） | ★ **是** | 装到板上 `/etc/systemd/system/` 与 `/etc/default/` |
| `build/` | 编译与上线脚本（6 个） | ★ **是** | `deploy.sh` 管 Agent 与语音 |
| `tests/` | 回归/探索/客户端（13 个） | ★ **是** | 改完代码必须跑前 4 个 |
| `ipc/` | IPC 延迟基准（学习线 M1） | 参考 | 结论已在 `docs/BENCHMARKS.md` |
| `tools/` | 攻坚工具/探针（54 个） | ⚠️ 归档 | 从板端抢救回来的排查工具，**基本不用动** |
| `drafts/` | 历史草稿（31 个） | ❌ **别动** | 结论已固化进 `docs/` |
| `scripts/` | 调试脚本（41 个） | ❌ **别动** | 记录排查过程，价值低 |
| `docs/` | 实验证据（4 份） | 参考 | 证据材料 |

#### `lab/agent/` 详解（最核心）

| 文件 | 行数级 | 是什么 | 板端路径 |
| :--- | :--- | :--- | :--- |
| `rkllm_embodied_agent.cpp` | ~2200 行 | **Agent 主体**：ReAct 循环 + 7 个硬件工具 + 6 个服务管控 + 快速通道 | `/root/rkllm_deploy/` |
| `edgevision_patrol_daemon.cpp` | ~1100 行 | 巡检守护（快层，纯 C 循环，不加载 LLM） | `/root/lab/` |
| `edgevision_agent_server.cpp` | ~400 行 | 8088 RPC 控制面（解耦数据面/控制面） | — |
| `verify_state.sh` | — | 守护运行态验证 | `/root/lab/` |
| `diag_patrol_launch.sh` | — | 守护启动诊断 | — |

> ⚠️ **`rkllm_embodied_agent.cpp` 是单文件 118 KB**，这是已知的技术债。
> 它同时承担：模型加载、工具实现、prompt 模板、意图路由、快速通道。
> **改动前务必先看该文件头部的注释块**，里面记了每个分支踩过的坑。

#### `lab/vision/` 详解

| 文件 | 是什么 | 板端二进制名 | 端口 |
| :--- | :--- | :--- | :--- |
| `edgevision_gateway.cpp` | **9000 TCP 定长帧网关**（248 B/帧） | `w4_edgevision_gateway` | 9000 |
| `edgevision_preview_real.cpp` | 8080 MJPEG 预览（1080p，3D 框 + BEV） | `w4_edgevision_preview_real` | 8080 |
| `cam_stream_server.cpp` | 学习期简化版 | — | 8080 |

> ⚠️ **仓库源码名与板上二进制名不同**（`edgevision_gateway.cpp` ↔ `w4_edgevision_gateway`）。
> `w4_` 前缀是第 4 周练习留下的，因为 systemd `ExecStart` 和多个 `pgrep -f` 依赖这个名字，
> **故意没改**。改名前请先全文搜索 `w4_edgevision`。

### `deploy/cpp/` — 视觉引擎 CMake 工程

**唯一有正规 CMake 构建的地方**（6 个 target）：

| Target | 产物 | 用途 |
| :--- | :--- | :--- |
| `edgevision_pro_rknn` | 板端 | 单张图全链路（MPP 硬解 → NPU → BEV） |
| `edgevision_dashboard` | 板端 | **8090 看板**（mp4 循环 / 摄像头） |
| `edgevision_rknn` | 板端 | 早期主程序 |
| `bench_dflfree` | 板端 | 性能量具（BENCHMARKS 的所有 NPU 数字） |
| `edgevision_x86` / `edgevision_pro` | PC | x86 编译（无 NPU，跑仿真） |

```
deploy/cpp/
├── CMakeLists.txt
├── src/          实现（postprocess_dfl_free.cpp 是精度评估公共组件）
├── include/      头文件（bev_projector.hpp / bev_render.hpp 是硬契约）
├── bench/        性能基准（所有数字的量具）
├── web/          dashboard.html（单文件前端，零外链）
└── 3rdparty/     rknn_api / mpp
```

> ⚠️ **不在 CMake 里的程序**：`lab/vision/*.cpp`、`lab/agent/*.cpp`、`lab/voice/*.cpp`
> 都是**手写 g++ 命令**编译的。这是技术债，见 `docs/OPEN_ISSUES.md`。

### `docs/` — 文档（17 MB，含图片）

**按这个顺序读**：

| 顺序 | 文件 | 回答什么问题 |
| :---: | :--- | :--- |
| 1 | `CURRENT_STATUS.md` | **现在能干什么** + 下一步（接手先看这份） |
| 2 | `ARCHITECTURE.md` | 三条技术线怎么串、端口进程表、资源争用 |
| 3 | `BENCHMARKS.md` | 所有数字的口径与复现命令（**引用数字只认这份**） |
| 4 | `TROUBLESHOOTING.md` | 踩过的坑（55 条，面试素材库） |
| 5 | `OPEN_ISSUES.md` | 未关闭问题清单（A/B/C 分类） |

```
docs/
├── tech_notes/     专项设计（Agent 工具契约 / 守护设计 / 语音链路…）
├── images/         证据图（README 和文档引用）
├── interview_book/ 面试材料（src/ 下有 14 篇）
├── archive/        早期设计稿（已标注"过期"）
├── workspace/      根目录文档的镜像（备份用）
└── resumes/        简历（.gitignore 屏蔽，含手机号）
```

---

## 三、独立工程（自成一体，不参与主线编译）

### `esp32_gimbal/` — ESP32-S3 云台执行器

**为什么单独存在**：EVM3588-B **没有任何 PWM 引出**，Linux 非实时内核也做不出稳定 50 Hz，
所以加一颗 ESP32-S3 当"实时执行器"，RK3588 只当"决策大脑"。

```
esp32_gimbal/
├── main/gimbal_main.c      ESP-IDF 固件
├── host/                   8 个 host 脚本 + 云台预览（76 KB）★
└── build.ps1               一键构建（自动躲开中文路径）
```

| host 脚本 | 干什么 |
| :--- | :--- |
| `w4_edgevision_gimbal_preview.cpp` | **云台预览**：8080 网页 + 串口驱动舵机 |
| `esp32_find.py` | 按 VID/PID 自动找 COM 口 |
| `gimbal_control.py` / `gimbal_tracker.py` | 两种控制律 |
| `verify_closed_loop.py` | 闭环验收 |
| `uvc_recover.sh` | **摄像头复位**（unbind/bind uvcvideo） |

> ⚠️ **两个同名 `build_preview.sh` 编的不是同一个程序**：
> - `esp32_gimbal/host/build_preview.sh` → 编 `w4_edgevision_gimbal_preview`（云台）
> - `lab/build/build_preview.sh` → 编 `w4_edgevision_preview_real`（8080 老预览）
>
> 板端 `/root/lab/build_preview.sh` 是**后者**。用①时必须写绝对路径。

### `stereo3d/` — 双目 3D

自带 CMake，**与主仓零代码依赖**，只抢摄像头。产物 `stereo3d_demo`，端口 8081。

### `convert/` — 模型转换

| 文件 | 在哪跑 | 干什么 |
| :--- | :--- | :--- |
| `export_dflfree_onnx.py` | PC | **图手术**：砍掉 DFL 头（228 节点，2 输出） |
| `export_onnx.py` | PC | 常规导出 |
| `build_calib_dataset.py` / `prepare_calib.py` | PC | 校准集构建（**决定量化精度生死**） |
| `eval_map.py` | PC | 真 mAP 评估（COCO128） |

> 转换本身在**板子上**跑（`/root/rknn_env`，toolkit 2.3.0），不依赖 PC/WSL。

### `data/` — 数据与模型（104 MB）

| 子目录 | 内容 | 大小 |
| :--- | :--- | :--- |
| `models/` | **`.rknn` 模型**（新的 4.34 MB / 旧的 6.22 MB） | ~15 MB |
| `videos/` | `highway_traffic.mp4`（8090 看板的输入，267 帧/圈） | 12 MB |
| `calib_v2/` | **真实校准集**（170 张，量化精度的关键） | — |
| `calib/` / `calib_real/` | 旧校准集（合成色块，**已弃用**，保留作反面教材） | — |
| `coco128/` | 128 张带标注（mAP 评估） | — |

> ⚠️ `calib/`（合成色块）不要再用 —— 它会让 cls 分支塌缩成常数 0.5。

---

## 四、历史遗留（**不要动，也不要基于它开发**）

这 7 个目录是 2026-09 的早期 Python 原型，**四者互相 import，已不是主线**：

| 目录 | 是什么 | 状态 |
| :--- | :--- | :--- |
| `gateway/` | Python 网关原型（`zero_copy_pipeline.py` 里 fd 是**模拟值**） | 已废弃 |
| `bev/` | BEV 投影原型（C++ 版在 `deploy/cpp/include/bev_projector.hpp`） | 已废弃 |
| `pipeline/` | 编排原型 | 已废弃 |
| `track/` | ByteTrack 原型（**C++ 里没实现跟踪**） | 已废弃 |
| `simulate/` | 仿真评估 | 已废弃 |
| `demo/` | PC 离线看板（**假数据**，页面顶部已加黄色声明条） | 仅供无硬件时看效果 |
| `outputs/` | 生成产物（mp4/log，26 MB） | 可删 |

**为什么留着不删**：`docs/archive/` 和 `docs/tech_notes/BEV_GATEWAY_ARCHITECTURE.md`
需要引用它们说明"早期设计 vs 最终实现"的差异。**它们是教材，不是代码。**

### 其他杂项

| 路径 | 是什么 | 能不能删 |
| :--- | :--- | :--- |
| `tools/` | 根 `_tools/` 的脱敏镜像（排查脚本） | 保留（设为 `BOARD_IP` 后用） |
| `scripts/` | 早期板端脚本（`check_board.sh` 等） | 保留 |
| `tests/` | 早期单测（`test_bev.py` / `test_gateway.py`） | 对应已废弃模块 |
| `learn/` | 第 1~4 周学习线代码（26 个，按周分） | **学习用，别删** |
| `_acl_report/` | 沙箱 ACL 诊断报告 | 可删 |
| `.tmp_resume/` | 临时目录（**未入库**） | 可删 |
| `deliverables/` | 简历解读 docx | 个人材料 |

---

## 五、构建与部署：文件在哪 → 板上在哪

**所有编译都必须在板上原生做**（交叉编译会撞 glibc：Ubuntu22 的 2.35 vs 板子 Debian11 的 2.31）。

| 仓库路径 | 板端路径 | 怎么编译 |
| :--- | :--- | :--- |
| `lab/agent/rkllm_embodied_agent.cpp` | `/root/rkllm_deploy/` | `bash lab/build/deploy.sh agent` |
| `lab/voice/voice_gateway.cpp` | `/root/` | `bash lab/build/deploy.sh gateway` |
| `lab/vision/edgevision_gateway.cpp` | `/root/lab/w4_edgevision_gateway.cpp` | `bash lab/build/build_w4_gateway.sh` |
| `lab/vision/edgevision_preview_real.cpp` | `/root/lab/w4_edgevision_preview_real.cpp` | `bash lab/build/build_preview.sh` |
| `lab/agent/edgevision_patrol_daemon.cpp` | `/root/lab/` | `bash lab/build/build_patrol_daemon.sh` |
| `deploy/cpp/` | `/root/edgevision/deploy/` | **CMake**（见下） |
| `lab/services/*.service` | `/etc/systemd/system/` | `cp` + `daemon-reload` |
| `lab/services/*.default` | `/etc/default/` | `cp` |

### 板端 CMake 编译

```bash
cd /root/edgevision/deploy/cpp
cmake -S . -B build_release -DCMAKE_BUILD_TYPE=Release -DCROSS_COMPILE_RK3588=ON
cmake --build build_release -j4
```

> ⚠️ **CMake 曾因 build type 为空跑了一个月 `-O0`**（TROUBLESHOOTING 第 24 条）。
> 现在 CMakeLists 已加自检：缺 `-O` 会 `message(WARNING)`。

---

## 六、端口与进程对照表

| 端口 | 子系统 | 进程 | systemd unit | 占摄像头 | 启动方式 |
| :--- | :--- | :--- | :--- | :---: | :--- |
| **8080** | 实时预览（MJPEG，1080p） | `w4_edgevision_preview_real` | `edgevision-preview` | ✅ | 手动/`control_service` |
| **8090** | 车辆检测看板（mp4） | `edgevision_dashboard` | `edgevision-dashboard` | ❌ | 同上 |
| **8090** | 车辆检测看板（摄像头） | 同上 | `edgevision-dashboard-cam` | ✅ | 同上 |
| **9000** | TCP 定长帧推流（248 B） | `w4_edgevision_gateway` | `edgevision` | ✅ | 同上 |
| **8088** | Agent RPC 控制面 | `edgevision_agent_server` | — | ❌ | 手动 |
| **8081** | 双目 stereo3d | `stereo3d_demo` | — | ✅（右目） | 手动 |
| — | 巡检守护（快层） | `edgevision_patrol_daemon` | `edgevision-patrol` | 只读探测 | systemd |
| — | 语音网关 | `voice_gateway` + `tts_daemon` | `voice-gateway` | ❌ | systemd |

### 互斥关系（**这是最容易踩的**）

| 抢什么 | 谁和谁 | 后果 | 现状 |
| :--- | :--- | :--- | :--- |
| **摄像头 `/dev/video83`** | 8080 / 8090-cam / 9000 | 后者静默假死（能 open 但没帧） | Agent 层 `fuser` 探测拦截 |
| **端口 8090** | dashboard(mp4) / dashboard_cam | 同端口冲突 | `Conflicts=` + Agent 拦截 |
| **NPU IOMMU 4 GiB** | Agent(2 GB) + 视觉(~200 MB) | 第 3 个 `errno 14` | `voice-gateway` 有 `Conflicts=` |
| 麦克风 `hw:0,0` | ASR / KWS / 录音 | 后来者 `exit 255` | unit 里 `fuser -k -9` |

**能同时跑**：8090(mp4) + 8080；8090(mp4) + 9000；巡检守护 + 任何。

---

## 七、改代码的规矩

### 改之前

1. **读 `AGENTS.md`** 的"绝对不能违反的"六条
2. **看 `docs/OPEN_ISSUES.md`** 别重复已有取舍
3. **搜同类代码**：同一个模式往往有 4 份副本（`cam_read` 就是例子）

### 改之后

```bash
# 板上跑回归（20 条，约 2 分钟）
python3 /root/regress.py
```

**必须同步三份文档**：

| 文档 | 补什么 |
| :--- | :--- |
| `docs/CURRENT_STATUS.md` | 现在能干什么 + 下一步 |
| `AGENTS.md` 第 3 节 | 一条带证据的 ✅ |
| `docs/BENCHMARKS.md` | 性能/精度数字（**必须写清口径**） |

### 命名约定

- 板上二进制名可能带 `w4_` 前缀（历史包袱），仓库源码名不带
- systemd unit 用 `edgevision-` 前缀，`edgevision.service` 是例外（9000 网关）

---

## 八、常见困惑速查

**Q: 我改了 `lab/vision/edgevision_gateway.cpp`，怎么生效？**
A: `scp` 到板上 `/root/lab/w4_edgevision_gateway.cpp` → 跑 `build_w4_gateway.sh` → `systemctl restart edgevision`。

**Q: 为什么有两个 8090？**
A: `edgevision-dashboard`（mp4）和 `edgevision-dashboard-cam`（摄像头）**同端口、不同 unit**，互斥。

**Q: `lab/drafts/` 和 `lab/scripts/` 能删吗？**
A: 技术上能，但它们是排查记录。**别删，也别改**——结论已经进 `docs/` 了。

**Q: `demo/` 里的数字是真的吗？**
A: **不是**。那是 PC 离线的假数据仿真，用于无硬件时看效果。页面顶部有黄色声明条。
**绝不能引用它的数字。**

**Q: 为什么 `lab/agent/rkllm_embodied_agent.cpp` 一个文件 118 KB？**
A: 技术债。它同时是模型加载器、7 个工具的实现、prompt 模板、意图路由器。
拆分已在 `docs/OPEN_ISSUES.md` 讨论过但未做（拆了要重编验证，收益要权衡）。

---

## 九、本文件怎么维护

**结构一变动就要改这里**，否则它会变成又一份过期文档（本项目已经吃过这个亏）。

具体触发条件：
- 新增/删除顶层目录
- 新增/改名 systemd unit
- 端口变化
- 主线 vs 归档的归属变化

**改的时候顺便核对** `README.md` 的目录表是否一致。
