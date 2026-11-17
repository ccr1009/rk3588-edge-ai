# 舵机「只抖一下/嗡嗡响但不转」诊断固件 —— 烧这个

> 现象：GPIO14 那颗底部舵机接上后**只抖一下 / 嗡嗡响但不转**。
> 本目录提供一个专门的诊断固件 + 上位机脚本，把
> **① 机械堵转 / ② 供电塌陷 / ③ 齿轮扫齿** 三种可能当场分开。
> 生成时间：2026-11-16

---

## 一、先看已经排除掉什么

信号侧**已经证明无罪**，不用再怀疑固件或引脚：

```
R 14 回环自测（ESP32 量自己 pad 上的真实电平）
  中位 0°   → pw_avg=1499us  per_avg=20000us
  +60°      → pw_avg=2165us  per_avg=20000us
  -60°      → pw_avg=832us   per_avg=20000us
```

1500/2165/832 µs、周期 20000 µs —— 标准 50Hz 舵机波形，完整覆盖 500~2500 µs。
命令链路也通：`I` → `GPIN 14 15`，`P`/`Q` 全部正常。

**所以问题一定在信号之外**，只剩三条：

| 可能 | 机理 | 症状 |
|:-:|---|---|
| ① 机械堵转 | 舵机顶到支架/底座硬限位，想继续转但转不动 | 嗡、发热、电流大 |
| ② 供电塌陷 | SG90 堵转 1.4A，USB/小 DC 模块一带载电压就掉 | 抖一下又弹回、嗡 |
| ③ 齿轮扫齿 | 内部塑料齿轮磨平，电机转但输出轴不动 | 电机嗡嗡空转、轴不动 |

三者症状一模一样，**必须靠电压和角度来分**，这就是这个固件干的事。

---

## 二、烧这个固件

### 方式 A（推荐）：把 ESP32 拔下来插 PC

```powershell
cd D:\a研究生\项目\rk3588项目\rk3588-edge-ai\esp32_gimbal

# 一键：烧写 + 自动跑全部 4 个实验 + 自动判读
& "C:\Users\15354\.espressif\python_env\idf6.0_py3.13_env\Scripts\python.exe" `
    host\flash_and_run_diag.py
```

脚本会自动找口（VID 303A / 1A86）、烧 `build\servo_diag_merged.bin`、
然后跑实验并把完整输出存到 `build\diag_result_T.txt`，最后打印判读。

指定端口 / 只跑某个实验：

```powershell
... host\flash_and_run_diag.py --port COM6
... host\flash_and_run_diag.py --no-flash --exp S   # 只扫行程
```

### 方式 B：ESP32 留在板子上

板子内核没编 USB-ACM，建不出 `/dev/ttyACM*`，只能 pyusb 烧，
成功率不高。**能拔就拔，别在这上面耗。**

如果坚持：

```bash
scp build/servo_diag_merged.bin root@<板子IP>:/root/
ssh root@<板子IP>
bash /root/esp32_gimbal/stop_bridge.sh          # 独占 USB
python3 /root/esp32_gimbal/esptool_over_usb.py  # 另开终端，按它打印的 pty 跑 esptool
```

### 手动烧（esptool 直接来）

```powershell
$py = "C:\Users\15354\.espressif\python_env\idf6.0_py3.13_env\Scripts\python.exe"
& $py -m esptool --chip esp32s3 --port COM6 -b 460800 `
    write-flash 0x0 build\servo_diag_merged.bin
```

> `servo_diag_merged.bin` 是**合并镜像**（bootloader + 分区表 + app），
> 从 `0x0` 烧一个文件就行。已校验首字节是 `0xE9`（ESP32 镜像魔数）。

### 烧回主固件

诊断完记得烧回去，否则云台控制链路是不通的：

```powershell
& $py -m esptool --chip esp32s3 --port COM6 -b 460800 `
    write-flash 0x0 build\esp32_gimbal_merged.bin
```

---

## 三、固件跑什么

发 `T` 依次跑 4 个实验（约 90 秒）：

| 实验 | 命令 | 做什么 | 能排除什么 |
|:-:|:-:|---|---|
| 1 | `B` | 中位静止 5 秒，采母线电压 | 建立电压基准 |
| 2 | `M` | ±10° 往返 8 次 | 幅度小到不可能堵转 → **纯测供电** |
| 3 | `S` | 全行程 -90°→+90°，每 6° 停 0.8s | 找「哪个角度开始嗡」= 堵转点 |
| 4 | `R` | 每 3° 停 1 秒爬坡逼近 | 电压在哪一度开始掉 |

其它命令：`A <pin>` 改脚、`P <deg>` 手动设角、`H` 回中、`V` 只读电压、`?` 帮助。

### 5V 电压怎么接（可选，但强烈建议）

不接也能跑，只是少了电压这一列，就只能靠肉眼。

```
外部 5V OUT+ ──┬── 100kΩ ──┬── GPIO1 (ADC1_CH0)
               │            │
           舵机红线       47kΩ
                            │
                          GND（与 ESP32 共地）
```

分压比 `47/(100+47) = 0.32`，5.0V 对应 1.60V，ADC 量程 3.1V 留足余量。

> ⚠️ **绝对不要把 5V 直接接 GPIO1**，会烧 ADC。

---

## 四、怎么判读

| 你看到 | 结论 | 怎么办 |
|---|---|---|
| 电压全程 ≥4.6V，某角度开始嗡/抖 | **① 机械堵转** | 调支架角度，或把 `TILT_RANGE_DEG` / `PAN_RANGE_DEG` 缩小 |
| 电压掉到 <4.3V（比基线低 0.6V 以上） | **② 供电不够** | 换大电流电源；5V 上并 **470µF 电解 + 0.1µF 瓷片**；舵机电源与 ESP32 分开走 |
| 电压正常、不堵转、电机响但轴不转 | **③ 齿轮扫齿** | 换 **MG90S**（金属齿轮，¥15/个，尺寸接线兼容） |

### 你这次最可能是哪个

「只抖一下 + 嗡嗡响」在这个项目里有很强的指向性：
**抖一下就回原位** = 舵机内部因欠压复位后重新上电 —— 这是 ② 供电塌陷的典型特征。
如果是 ① 堵转，通常是**持续**嗡嗡响并发热，不会「抖一下就停」。

先按下面的顺序花两分钟排除：

1. **拔掉舵机，只量电源**：红线↔棕线应有 4.8~5.2V
2. **接上舵机再量**：如果掉了 0.5V 以上，就是电源内阻大
3. **Command 一次全行程**，同时盯着电压看它掉到多少
4. 以上都正常还只嗡不转 → 才轮到换舵机

---

## 五、文件清单

| 文件 | 作用 |
|---|---|
| `diag_power/main/diag_main.c` | 诊断固件源码 |
| `diag_power/sdkconfig.defaults` | 固件配置（esp32s3，双控制台） |
| `build_diag.ps1` | 构建脚本（复用主工程的 IDF 环境绕坑逻辑） |
| `build/servo_diag_merged.bin` | **烧这个**（合并镜像，地址 0x0） |
| `build/servo_diag.bin` | 单独 app 镜像（给 idf.py flash 用） |
| `build/esp32_gimbal_merged.bin` | 主固件恢复镜像，烧完诊断用这个烧回去 |
| `host/flash_and_run_diag.py` | PC 侧：烧写 + 跑实验 + 自动判读 |
| `host/run_diag_from_board.py` | 板端：ESP32 插板子时跑实验 |

构建：

```powershell
pwsh -File build_diag.ps1 -Doctor   # 体检环境
pwsh -File build_diag.ps1           # 只构建
pwsh -File build_diag.ps1 -Flash    # 构建 + 烧写
```
