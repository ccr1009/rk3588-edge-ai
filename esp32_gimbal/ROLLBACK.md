# 云台追踪改动回滚手册

> 建立时间：2026-11-17 09:20
> 目的：改动后效果不好时，能在 2 分钟内退回到改动前。

---

## 一、两个回滚锚点（git tag）

| tag | 指向 | 内容 |
|---|---|---|
| `gimbal-before-opt` | `af9d098` | **四项优化前的状态**，回到这里等于撤销昨晚全部改动 |
| `gimbal-opt-v1` | `5f17e10` | 四项优化 v1（P1/P3/P4 已验证，P2 固件待烧） |

仓库：`D:\a研究生\项目\rk3588项目\rk3588-edge-ai`

---

## 二、三种回滚场景

### 场景 A：只想撤销某个文件（最常用）

```bash
cd D:\a研究生\项目\rk3588项目\rk3588-edge-ai

# 撤销单个文件到改动前
git checkout gimbal-before-opt -- esp32_gimbal/main/gimbal_main.c

# 撤销全部改动
git checkout gimbal-before-opt -- esp32_gimbal/
```

### 场景 B：整个仓库退回改动前（激进）

```bash
cd D:\a研究生\项目\rk3588项目\rk3588-edge-ai

# 先看差异（不改任何东西）
git diff gimbal-before-opt HEAD --stat

# 确认后硬重置（会丢失未提交的改动！）
git reset --hard gimbal-before-opt

# 或者用 revert（保留历史，更安全）
git revert --no-commit gimbal-before-opt..HEAD
git commit -m "revert: 撤销云台四项优化"
```

### 场景 C：只回滚板子上跑的二进制（不回滚源码）

这是**最快**的回滚方式，不用重新编译：

```bash
# 板上有历史版本存档
ssh rk3588 "ls -l /root/lab/_archive_20261116/"

# 回滚到四项优化之前（11-16 14:19 那个）
ssh rk3588 "cd /root/lab && cp _archive_20261116/w4_edgevision_gimbal_preview.v1_原始 ./w4_edgevision_gimbal_preview.rollback"

# 停服务，用回滚版启动
ssh rk3588 "sudo systemctl stop gimbal-tracker
sed -i 's|\.final|.rollback|' /etc/systemd/system/gimbal-tracker.service
sudo systemctl daemon-reload && sudo systemctl start gimbal-tracker"
```

---

## 三、各改动点单独回滚方法

如果只想撤销**某一项**优化，而不是全部：

### 撤销 P1（帧间插值）

最简单：**让程序不发 `S 40`**。

```bash
# 源码里删掉这段（在 GimbalSerial::open_tcp 里）
ssh rk3588 "grep -n 'S 40' /root/lab/w4_edgevision_gimbal_preview.cpp"
```

或者不重编译，直接手动覆盖：
```bash
# 程序启动后手动发 S 0
ssh rk3588 "/root/rknn_env/bin/python -c \"
import socket, time
s = socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 9100))
s.sendall(b'S 0\\n'); time.sleep(0.3); print(s.recv(64).decode().strip())
s.close()\""
```

### 撤销 P3（异步回包）

用命令行参数控制不可行（改的是代码逻辑）。需要重编译旧版：

```bash
git checkout gimbal-before-opt -- esp32_gimbal/host/w4_edgevision_gimbal_preview.cpp
# 然后按下方「重编译」章节操作
```

### 撤销 P4（流水线）

如果流水线有问题，用旧二进制即可：
```bash
ssh rk3588 "cd /root/lab && ls -l w4_edgevision_gimbal_preview.v2"
# v2 是流水线之前的串行版本（28.4 FPS / 39ms 延迟）
```

### 撤销 P2（333Hz 固件）

**如果已经烧了 333Hz 想退回 50Hz**：

```powershell
# ESP32 插 PC，烧回旧固件
& "C:\Users\15354\.espressif\python_env\idf6.0_py3.13_env\Scripts\python.exe" `
    -m esptool --chip esp32s3 --port COM6 -b 460800 `
    write-flash 0x0 D:\a研究生\项目\rk3588项目\rk3588-edge-ai\esp32_gimbal\build\esp32_gimbal_merged.bin
```

`esp32_gimbal_merged.bin` 是 11-16 18:48 的固件（50Hz），是安全的回滚点。

---

## 四、重编译方法

改了源码之后，怎么重新生成二进制：

### C++（RK3588 板上编译）

```bash
ssh rk3588 "cd /root/lab && g++ -O2 -std=c++17 w4_edgevision_gimbal_preview.cpp \\
  /root/edgevision/deploy/cpp/src/postprocess_dfl_free.cpp \\
  -o /root/lab/w4_edgevision_gimbal_preview.final \\
  -I/root/lab -I/root/edgevision/deploy/cpp/include \\
  -I/root/edgevision/deploy/cpp/3rdparty/rknn_api/include \\
  \$(pkg-config --cflags --libs opencv4) -lrknnrt -lpthread"
```

**注意**：本地仓库的源码和板上源码是**两份**。改哪份要搞清楚：
- 板上的：`/root/lab/w4_edgevision_gimbal_preview.cpp`（实际编译用）
- 仓库的：`esp32_gimbal/host/w4_edgevision_gimbal_preview.cpp`（版本控制用）

改完要么同步过去，要么直接在板上改完后拉回来提交。

### ESP32 固件（PC 上编译）

```powershell
cd D:\a研究生\项目\rk3588项目\rk3588-edge-ai\esp32_gimbal
pwsh -File build.ps1              # 只编译
pwsh -File build.ps1 -Flash       # 编译+烧录
```

---

## 五、验证改动是否生效

```bash
# 一键验证四项 + 稳定性
ssh rk3588 "bash /root/esp32_gimbal/verify_after_flash.sh"

# 单独看实时指标
ssh rk3588 "curl -s http://127.0.0.1:8080/status"

# 看分段耗时（判断流水线是否正常）
ssh rk3588 "tail -5 /tmp/gimbal_v2.log | grep profile"
```

**正常基线**（白天光照充足时）：

```
fps=30.00  npu_ms=17.2  errors=0  dropped=0
A 33.3 | B 20.3 | C 8.5 | 延迟 22.0~22.7 ms
```

**注意**：晚上测会看到 20~27 fps，那是相机自动曝光在暗光下延长曝光时间，
不是改动失效。这一点在 `OPTIMIZATION_20261116.md` 里有实测记录
（白天亮度 98.3 → 30fps，凌晨亮度 0.1 → 20fps）。

---

## 六、出错时的诊断顺序

按这个顺序排查，别跳步：

1. **服务起了吗**
   ```bash
   ssh rk3588 "systemctl is-active gimbal-bridge gimbal-tracker"
   ```

2. **端口通吗**
   ```bash
   ssh rk3588 "ss -ltn | grep -E '8080|9100'"
   ```

3. **看日志有无报错**
   ```bash
   ssh rk3588 "tail -50 /tmp/gimbal_v2.log"
   ssh rk3588 "tail -20 /tmp/gimbal_bridge.log"
   ```

4. **云台能通吗**
   ```bash
   ssh rk3588 "/root/rknn_env/bin/python -c \"
   import socket, time
   s = socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 9100))
   s.sendall(b'I\\n'); time.sleep(0.3)
   print(s.recv(64).decode().strip())   # 应该回 GPIN 14 15
   s.close()\""
   ```

5. **还是不对就用场景 C 回滚二进制**

---

## 七、⚠️ 遗留问题（必须知道）

1. **板上源码和仓库源码是两份，不自动同步**
   板上：`/root/lab/w4_edgevision_gimbal_preview.cpp`
   仓库：`esp32_gimbal/host/w4_edgevision_gimbal_preview.cpp`
   **以后改代码要记得两边同步**，否则下次又会脱节。

2. **P2 固件还没烧**
   `gimbal_main.c` 的 333Hz 改动已提交，但板子上跑的还是 50Hz 旧固件。
   烧录方法见 `HANDOVER_20261117.md`。

3. **`SIGTERM` 杀不死**
   程序 HTTP 线程卡在 `accept()`，用 systemd 的 `KillSignal=SIGKILL` 绕过。
   手动 `pkill` 时要带 `-9`。

4. **服务已 disable 开机自启**
   （因为用户要求深夜静音）需要时手动：
   ```bash
   ssh rk3588 "sudo systemctl enable --now gimbal-bridge gimbal-tracker"
   ```
