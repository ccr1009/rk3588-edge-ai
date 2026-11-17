# 构建并烧写舵机诊断固件 servo_diag（ESP-IDF v6.0.1）
#
# 用法:
#   pwsh -File build_diag.ps1                  # 只构建，产物在 .\build\servo_diag.bin
#   pwsh -File build_diag.ps1 -Flash           # 构建 + 烧写（自动找端口）
#   pwsh -File build_diag.ps1 -Flash -Port COM6
#   pwsh -File build_diag.ps1 -Monitor         # 烧完直接看串口
#   pwsh -File build_diag.ps1 -Doctor          # 只体检环境
#
# 这个脚本和主工程的 build.ps1 用的是同一套环境绕坑逻辑（IDF 环境、中文路径、
# 工具目录布局），只是工程目录换成 diag_power\、构建区换成 C:\gimbal_diag\src。

param(
    [switch]$Clean,
    [switch]$Flash,
    [switch]$Monitor,
    [switch]$Doctor,
    [string]$Port = "",
    [string]$BuildDir = "C:\gimbal_diag\src"     # 必须是纯 ASCII 路径
)

$ErrorActionPreference = "Stop"

# ══════════════ 配置（换机器只改这两行） ══════════════
$idfPath   = "C:\Users\15354\esp\esp-idf-v6.0.1"
$toolsPath = "C:\Users\15354\.espressif"
# ═══════════════════════════════════════════════════════

$pyEnv   = Join-Path $toolsPath "python_env\idf6.0_py3.13_env"
$projDir = Join-Path $PSScriptRoot "diag_power"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ---------------------------------------------------------------- 环境
$env:IDF_PATH            = $idfPath
$env:IDF_TOOLS_PATH      = $toolsPath
$env:IDF_PYTHON_ENV_PATH = $pyEnv
$env:IDF_TARGET          = "esp32s3"
$env:ESP_IDF_VERSION     = "6.0.1"

$romRoot = Join-Path $toolsPath "tools\esp-rom-elfs"
if (Test-Path $romRoot) {
    $ver = Get-ChildItem $romRoot -Directory -ErrorAction SilentlyContinue |
           Sort-Object Name -Descending | Select-Object -First 1
    if ($ver) { $env:ESP_ROM_ELF_DIR = $ver.FullName }
}

$py    = Join-Path $pyEnv "Scripts\python.exe"
$idfPy = Join-Path $idfPath "tools\idf.py"

$found = New-Object System.Collections.ArrayList
foreach ($tool in (Get-ChildItem (Join-Path $toolsPath "tools") -Directory -ErrorAction SilentlyContinue)) {
    Get-ChildItem $tool.FullName -Recurse -Depth 3 -Directory -Filter "bin" -ErrorAction SilentlyContinue |
        ForEach-Object { if (-not $found.Contains($_.FullName)) { [void]$found.Add($_.FullName) } }
    Get-ChildItem $tool.FullName -Recurse -Depth 3 -Filter "*.exe" -ErrorAction SilentlyContinue |
        ForEach-Object {
            $d = $_.DirectoryName
            if (-not $found.Contains($d)) { [void]$found.Add($d) }
        }
}
foreach ($d in $found) { $env:PATH = "$d;$env:PATH" }

# ---------------------------------------------------------------- 体检
if ($Doctor) {
    Step "环境体检"
    $checks = @(
        @{ N = "IDF 路径";    P = $idfPath;  T = "Directory" },
        @{ N = "idf.py";      P = $idfPy;    T = "Leaf" },
        @{ N = "python env";  P = $py;       T = "Leaf" },
        @{ N = "xtensa gcc";  P = (Get-Command "xtensa-esp32s3-elf-gcc" -ErrorAction SilentlyContinue).Source; T = "Leaf" },
        @{ N = "cmake";       P = (Get-Command "cmake" -ErrorAction SilentlyContinue).Source;                  T = "Leaf" },
        @{ N = "ninja";       P = (Get-Command "ninja" -ErrorAction SilentlyContinue).Source;                  T = "Leaf" },
        @{ N = "rom elf";     P = $env:ESP_ROM_ELF_DIR; T = "Directory" },
        @{ N = "diag 源目录"; P = $projDir; T = "Directory" }
    )
    $bad = 0
    foreach ($c in $checks) {
        $ok = if ($c.T -eq "Directory") { Test-Path $c.P -PathType Container } else { $c.P -and (Test-Path $c.P) }
        if ($ok) { Say ("  [OK]   {0,-12} {1}" -f $c.N, $c.P) "Green" }
        else     { Say ("  [MISS] {0,-12} {1}" -f $c.N, $c.P) "Red"; $bad++ }
    }
    Say ("`n  IDF_PYTHON_ENV = {0}" -f $env:IDF_PYTHON_ENV_PATH)
    Say ("  ESP_IDF_VERSION = {0}" -f $env:ESP_IDF_VERSION)
    Say ("  构建区 = {0}" -f $BuildDir)
    if ($bad -gt 0) { Say "`n  $bad 项缺失，先修环境" "Red"; exit 1 }
    Say "`n  体检通过" "Green"
    exit 0
}

if (-not (Test-Path $py))    { Say "找不到 IDF python: $py" "Red"; exit 1 }
if (-not (Test-Path $idfPy)) { Say "找不到 idf.py: $idfPy" "Red"; exit 1 }
if (-not (Test-Path $projDir)) { Say "找不到诊断工程目录: $projDir" "Red"; exit 1 }

# ---------------------------------------------------------------- 同步到 ASCII 构建区
Step "同步源码 -> ASCII 构建区（规避 cmake 中文路径崩溃）"
Say ("  {0}" -f $projDir)
Say ("       ->  {0}" -f $BuildDir)
if (-not (Test-Path $BuildDir)) { New-Item -ItemType Directory -Path $BuildDir -Force | Out-Null }

$srcMain = Join-Path $projDir "main"
$dstMain = Join-Path $BuildDir "main"
if (-not (Test-Path $dstMain)) { New-Item -ItemType Directory -Path $dstMain -Force | Out-Null }
Get-ChildItem $srcMain -File | ForEach-Object { Copy-Item $_.FullName (Join-Path $dstMain $_.Name) -Force }
foreach ($f in @("CMakeLists.txt", "sdkconfig.defaults")) {
    $s = Join-Path $projDir $f
    if (Test-Path $s) { Copy-Item $s (Join-Path $BuildDir $f) -Force }
}
$sc = Join-Path $projDir "sdkconfig"
if (Test-Path $sc) { Copy-Item $sc (Join-Path $BuildDir "sdkconfig") -Force }
Say "  同步完成" "Green"

Set-Location $BuildDir

if ($Clean) {
    Step "清理"
    if (Test-Path "build")     { Remove-Item -Recurse -Force "build"; Say "  删除 build/" }
    if (Test-Path "sdkconfig") { Remove-Item -Force "sdkconfig";      Say "  删除 sdkconfig" }
}

# ---------------------------------------------------------------- 构建
Step "构建 servo_diag"
& $py $idfPy -DIDF_TARGET=esp32s3 build
if ($LASTEXITCODE -ne 0) { Say "构建失败 exit=$LASTEXITCODE" "Red"; exit $LASTEXITCODE }

$binPath = Join-Path $BuildDir "build\servo_diag.bin"
if (-not (Test-Path $binPath)) { Say "构建声称成功但找不到产物: $binPath" "Red"; exit 1 }
Say ("`n构建成功  固件 {0:N0} bytes" -f (Get-Item $binPath).Length) "Green"
Say ("  产物: {0}" -f $binPath)

$outDir = Join-Path $PSScriptRoot "build"
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
Copy-Item $binPath (Join-Path $outDir "servo_diag.bin") -Force
Say ("  已回拷到: {0}" -f (Join-Path $outDir "servo_diag.bin"))

# ---------------------------------------------------------------- 找端口
if ($Flash -and -not $Port) {
    Step "自动检测 ESP32 串口（按 VID/PID）"
    $cands = @()
    try {
        $devs = Get-CimInstance Win32_PnPEntity -ErrorAction Stop |
                Where-Object { $_.Name -match 'COM\d+' }
        foreach ($dev in $devs) {
            $m = [regex]::Match($dev.Name, 'COM(\d+)')
            if (-not $m.Success) { continue }
            $com = "COM" + $m.Groups[1].Value
            $vm = [regex]::Match($dev.DeviceID, 'VID_([0-9A-Fa-f]{4})')
            if ($vm.Success -and ($vm.Groups[1].Value.ToUpper() -in @("303A", "1A86"))) {
                $cands += [pscustomobject]@{ Com = $com; Name = $dev.Name }
            }
        }
    } catch { }

    if ($cands.Count -eq 0) {
        Say "  没找到 ESP32（VID_303A 或 VID_1A86）。USB 线插了吗？" "Red"
        Say "  可用端口：" "Yellow"
        [System.IO.Ports.SerialPort]::GetPortNames() | ForEach-Object { Say "    $_" }
        exit 1
    }
    foreach ($c in $cands) { Say ("  {0}  {1}" -f $c.Com, $c.Name) }
    $Port = $cands[0].Com
}

if ($Flash) {
    Step "烧写到 $Port"
    & $py $idfPy -p $Port flash
    if ($LASTEXITCODE -ne 0) { Say "烧写失败 exit=$LASTEXITCODE" "Red"; exit $LASTEXITCODE }
    Say "`n烧写完成" "Green"
    Say "  验证：串口应看到  BOOT servo_diag v1 / READY pin=14 adc=1 range=+-90deg"
    Say "  然后发 T 跑全部实验"
}

if ($Monitor) {
    Step "串口监视（Ctrl+] 退出）"
    & $py $idfPy -p $Port monitor
}
