# 视觉追踪启动脚本（参数已实测标定）
#
# 用法：
#   pwsh -File run_tracking.ps1                    # 默认跑 30 秒
#   pwsh -File run_tracking.ps1 -Seconds 60        # 跑久一点
#   pwsh -File run_tracking.ps1 -DryRun            # 只算不发，调参用
#
# ══════════════════════════════════════════════════════════════════════
# 这些参数是怎么定下来的（2026-11-03 实测，别随便改）
# ══════════════════════════════════════════════════════════════════════
#
# ① --invert-pan / --invert-tilt  ★ 必须加
#    摄像头装到云台平台上之后，两个轴的方向都和「标准安装」相反：
#      云台右转 → 目标在画面里反而往右跑 → 正反馈 → 顶死限位
#    实测对比（同一场景，25 秒）：
#      不加 invert    : dx 恒在 +0.33，pan 一路累加到 +90 顶死
#      只加 invert-pan: dx 多次归零（收敛了），但 tilt 仍顶在 -45
#      两个都加       : dx=0.000 dy=0.000 同时出现，角度稳定不漂  ✅
#
#    为什么方向会反：舵机绕线方向 / 固件 PAN_INVERT / 摄像头倒装 三者
#    在软件层无法区分，也不需要区分 —— 加符号翻转比拆硬件便宜。
#
# ② --kp-pan 0.20 --kp-tilt 0.15  （默认值是 0.45/0.40，太大）
#    摄像头装在云台上时，系统是「积分器 + 反馈延迟」，
#    等效增益 = kp × 摄像头反馈增益，默认值会立刻振荡。
#
# ③ --max-step 2.5  （默认 6.0 太快）
#    17 Hz × 2.5° = 42°/秒，给画面反馈留出时间。
#    默认 6.0 时是 102°/秒，1 秒就能从中间转到极限。
#
# ④ --min-score 0.4
#    人站在 1~2 米时置信度实测 0.85+，0.4 足够过滤误检。
#    画面暗或人远时会掉到 0.3，那时再降到 0.25。
#
# ── 已知限制 ────────────────────────────────────────────────────────
# pan 行程 ±90°，目标跑到云台转不到的方位会顶死（实测出现 99 次）。
# 解法：把云台底座转一下，让 0° 位对着人常待的方向。

param(
    [int]$Seconds = 30,
    [switch]$DryRun,
    [string]$Port = "",
    [string]$BoardHost = "<board-ip>",
    [double]$KpPan = 0.20,
    [double]$KpTilt = 0.15,
    [double]$MaxStep = 2.5,
    [double]$MinScore = 0.4,
    [double]$PanRange = 90.0
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$py = "C:\Users\15354\.espressif\python_env\idf6.0_py3.13_env\Scripts\python.exe"

if (-not (Test-Path $py)) { Write-Host "找不到 python: $py" -ForegroundColor Red; exit 1 }

# 自动找 ESP32 端口
# 不用 esp32_find.py 的输出（中文在 PowerShell 里会变成乱码，正则匹配可靠不了），
# 直接查 PnP 设备的 VID。303A=Espressif 原生USB，1A86=CH340/CH343。
if (-not $Port) {
    Write-Host "=== 自动检测 ESP32 端口 ===" -ForegroundColor Cyan
    $cands = @()
    try {
        Get-CimInstance Win32_PnPEntity -ErrorAction Stop |
            Where-Object { $_.Name -match 'COM\d+' } |
            ForEach-Object {
                $mm = [regex]::Match($_.Name, 'COM(\d+)')
                if (-not $mm.Success) { return }
                $vm = [regex]::Match($_.DeviceID, 'VID_([0-9A-Fa-f]{4})')
                if (-not $vm.Success) { return }
                $vid = $vm.Groups[1].Value.ToUpper()
                if ($vid -eq '303A' -or $vid -eq '1A86') {
                    $cands += [pscustomobject]@{
                        Com = "COM" + $mm.Groups[1].Value
                        Vid = $vid
                        Name = $_.Name
                    }
                }
            }
    } catch { }

    if ($cands.Count -eq 0) {
        Write-Host "  没找到 ESP32（VID_303A / VID_1A86），USB 线插了吗？" -ForegroundColor Red
        Write-Host "  当前可用端口：" -ForegroundColor Yellow
        [System.IO.Ports.SerialPort]::GetPortNames() | ForEach-Object { Write-Host "    $_" }
        exit 1
    }
    foreach ($c in $cands) {
        Write-Host ("  {0}  VID_{1}  {2}" -f $c.Com, $c.Vid, $c.Name)
    }
    $Port = $cands[0].Com
    Write-Host "  使用端口 $Port" -ForegroundColor Green
}

# 检查板子网关
Write-Host "`n=== 检查板子 9000 网关 ===" -ForegroundColor Cyan
$sshOut = (ssh -o ConnectTimeout=6 "root@$BoardHost" "systemctl is-active edgevision; ss -tln | grep -c :9000" 2>&1) -join "`n"
$sshOut.Trim().Split("`n") | ForEach-Object { Write-Host "  $_" }
# 两个条件都要满足：服务 active 且 9000 在监听
$svcOk = $sshOut -match "active"
$portOk = $sshOut -match "(?m)^[1-9]\d*$"
if (-not ($svcOk -and $portOk)) {
    Write-Host "  网关未就绪（active=$svcOk port=$portOk），尝试启动..." -ForegroundColor Yellow
    ssh -o ConnectTimeout=6 "root@$BoardHost" "systemctl restart edgevision" 2>&1 | Out-Null
    Start-Sleep -Seconds 8
}

Write-Host "`n=== 开始追踪 ($Seconds 秒) ===" -ForegroundColor Cyan
Write-Host ("  参数: kp=({0:N2}, {1:N2})  max_step={2:N1}  min_score={3:N2}  invert-pan+tilt" -f $KpPan, $KpTilt, $MaxStep, $MinScore)
Write-Host "  按 Ctrl-C 提前结束`n"

$args = @(
    "$here\verify_closed_loop.py",
    "--port", $Port,
    "--host", $BoardHost,
    "--seconds", $Seconds,
    "--mode", "relative",
    "--min-score", $MinScore,
    "--kp-pan", $KpPan,
    "--kp-tilt", $KpTilt,
    "--max-step", $MaxStep,
    "--pan-range", $PanRange,
    "--invert-pan",
    "--invert-tilt"
)

$env:PYTHONIOENCODING = "utf-8"
& $py @args

Write-Host "`n提示：如果出现大量「顶到限位」，说明目标跑出云台行程了。" -ForegroundColor Yellow
Write-Host "      解法：把云台底座转向人常待的方向，或加 -PanRange 60 限制范围。"
