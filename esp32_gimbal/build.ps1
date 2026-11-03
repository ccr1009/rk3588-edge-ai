# 构建并烧写 esp32_gimbal（ESP-IDF v6.0.1）
#
# 用法:
#   pwsh -File build.ps1                      # 只构建
#   pwsh -File build.ps1 -Flash               # 构建 + 烧写（自动找端口）
#   pwsh -File build.ps1 -Flash -Port COM6    # 指定端口
#   pwsh -File build.ps1 -Clean -Flash        # 全量重建
#   pwsh -File build.ps1 -Doctor              # 只体检环境，不构建
#
# ══════════════════════════════════════════════════════════════════════════
# 为什么要有这个脚本，以及它绕开了哪些坑
# ══════════════════════════════════════════════════════════════════════════
#
# 【坑 1】export.ps1 是坏的
#   本机 IDF v6.0.1 的 export.ps1 会去找 python_env\idf6.0_py3.12_env，
#   但实际装的是 idf6.0_py3.13_env（Python 3.13）。版本号写死了，载入失败。
#   → 不修 IDF 安装本身，直接手动设环境变量后调用 idf.py。
#
# 【坑 2】绕过 export.ps1 会丢 ESP_IDF_VERSION
#   export.ps1 本来会设它，绕过后 idf_component_manager 解析版本拿到 None：
#     TypeError: expected string or bytes-like object, got 'NoneType'
#   → 手动补 $env:ESP_IDF_VERSION = "6.0.1"。
#
# 【坑 3】.espressif\tools 布局不统一（实测三种）
#     ninja\1.12.1\ninja.exe                             → 版本目录直接放 exe
#     cmake\4.0.3\bin\cmake.exe                          → 版本目录下还有 bin
#     xtensa-esp-elf\esp-15.2.0_xxx\xtensa-esp-elf\bin\  → 多套一层同名目录
#   硬编码层级必漏 → 递归扫含 .exe 的目录。
#
# 【坑 4】★ 中文路径让 cmake 直接崩（这个最坑，排查成本最高）
#   项目在 D:\a研究生\项目\rk3588项目\... 下，cmake 报：
#     cmake failed with exit code 3221226505   (0xC0000409 STATUS_STACK_BUFFER_OVERRUN)
#   根因：Windows 上 cmake/ninja 用窄字符(ANSI) API 处理路径，
#        中文经 UTF-8→GBK→UTF-8 多次转换后越界。IDF 构建脚本没做编码保护。
#   这是**工具链缺陷，不是代码问题**，改不了 cmake。
#   → 自动把源码同步到 ASCII 路径构建，产物再拷回来。用户无感。
#
# 坑 1~3 是环境问题、坑 4 是工具链缺陷 —— 四条都已经在脚本里处理掉了。
# 换台机器只要改下面 $idfPath / $toolsPath 两行即可。

param(
    [switch]$Clean,
    [switch]$Flash,
    [switch]$Monitor,
    [switch]$Doctor,
    [string]$Port = "",              # 留空 = 自动检测
    [string]$BuildDir = "C:\gimbal_build\src",   # 必须是纯 ASCII 路径
    [switch]$KeepBuildDir            # 保留构建工作区（默认保留，方便增量编译）
)

$ErrorActionPreference = "Stop"

# ══════════════ 配置（换机器只改这两行） ══════════════
$idfPath   = "C:\Users\15354\esp\esp-idf-v6.0.1"
$toolsPath = "C:\Users\15354\.espressif"
# ═══════════════════════════════════════════════════════

$pyEnv   = Join-Path $toolsPath "python_env\idf6.0_py3.13_env"
$projDir = $PSScriptRoot

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ---------------------------------------------------------------- 环境
$env:IDF_PATH            = $idfPath
$env:IDF_TOOLS_PATH      = $toolsPath
$env:IDF_PYTHON_ENV_PATH = $pyEnv
$env:IDF_TARGET          = "esp32s3"
$env:ESP_IDF_VERSION     = "6.0.1"                              # 坑 2

# esp-rom-elfs 的版本子目录名随 IDF 安装而变（本机实测是 20241011，
# 不是网上常见的 20250925）。硬编码必错，所以动态取最新那个。
$romElfs = $null
$romRoot = Join-Path $toolsPath "tools\esp-rom-elfs"
if (Test-Path $romRoot) {
    $ver = Get-ChildItem $romRoot -Directory -ErrorAction SilentlyContinue |
           Sort-Object Name -Descending | Select-Object -First 1
    if ($ver) { $romElfs = $ver.FullName }
}
if ($romElfs) { $env:ESP_ROM_ELF_DIR = $romElfs }

$py = Join-Path $pyEnv "Scripts\python.exe"
$idfPy = Join-Path $idfPath "tools\idf.py"

# PATH 递归发现（坑 3）
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
        @{ N = "IDF 路径";        P = $idfPath;                                    T = "Directory" },
        @{ N = "idf.py";          P = $idfPy;                                      T = "Leaf" },
        @{ N = "python env";      P = $py;                                         T = "Leaf" },
        @{ N = "xtensa gcc";      P = (Get-Command "xtensa-esp32s3-elf-gcc" -ErrorAction SilentlyContinue).Source; T = "Leaf" },
        @{ N = "cmake";           P = (Get-Command "cmake" -ErrorAction SilentlyContinue).Source;                  T = "Leaf" },
        @{ N = "ninja";           P = (Get-Command "ninja" -ErrorAction SilentlyContinue).Source;                  T = "Leaf" },
        @{ N = "rom elf";         P = $env:ESP_ROM_ELF_DIR;                        T = "Directory" }
    )
    $bad = 0
    foreach ($c in $checks) {
        $ok = if ($c.T -eq "Directory") { Test-Path $c.P -PathType Container } else { $c.P -and (Test-Path $c.P) }
        if ($ok) { Say ("  [OK]   {0,-14} {1}" -f $c.N, $c.P) "Green" }
        else     { Say ("  [MISS] {0,-14} {1}" -f $c.N, $c.P) "Red"; $bad++ }
    }

    # 中文路径检查（坑 4）
    $hasCJK = $projDir -match '[^\x00-\x7F]'
    if ($hasCJK) {
        Say ("  [注意] 项目路径含非 ASCII 字符 -> 会自动走 ASCII 构建区：{0}" -f $BuildDir) "Yellow"
    } else {
        Say "  [OK]   项目路径是纯 ASCII，可以直接原地构建" "Green"
    }

    Say ("`n  IDF_PYTHON_ENV = {0}" -f $env:IDF_PYTHON_ENV_PATH)
    Say ("  ESP_IDF_VERSION = {0}" -f $env:ESP_IDF_VERSION)
    Say ("  PATH 附加目录数 = {0}" -f $found.Count)
    if ($bad -gt 0) { Say "`n  $bad 项缺失，先修环境" "Red"; exit 1 }
    Say "`n  体检通过" "Green"
    exit 0
}

# ---------------------------------------------------------------- 校验
if (-not (Test-Path $py))    { Say "找不到 IDF python: $py" "Red"; exit 1 }
if (-not (Test-Path $idfPy)) { Say "找不到 idf.py: $idfPy" "Red"; exit 1 }

$gcc = Get-Command "xtensa-esp32s3-elf-gcc" -ErrorAction SilentlyContinue
if (-not $gcc) { Say "找不到 xtensa-esp32s3-elf-gcc（跑 -Doctor 看详情）" "Red"; exit 1 }

Step "环境"
Say ("  编译器        {0}" -f $gcc.Source)
Say ("  IDF           {0}" -f $env:IDF_PATH)
Say ("  python        {0}" -f (& $py -c "import sys; print(sys.version.split()[0])"))

# ---------------------------------------------------------------- 同步到 ASCII 构建区（坑 4）
$needAscii = ($projDir -match '[^\x00-\x7F]') -or ($projDir -ne $BuildDir)
if ($needAscii) {
    Step "同步源码 -> ASCII 构建区（规避 cmake 中文路径崩溃）"
    Say ("  {0}" -f $projDir)
    Say ("       ->  {0}" -f $BuildDir)

    foreach ($sub in @("main", "host")) {
        $s = Join-Path $projDir $sub
        $d = Join-Path $BuildDir $sub
        if (Test-Path $s) {
            if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
            # 只同步源码，不同步 build（否则会带上中文路径的绝对路径缓存）
            Get-ChildItem $s -File | ForEach-Object {
                Copy-Item $_.FullName (Join-Path $d $_.Name) -Force
            }
        }
    }
    foreach ($f in @("CMakeLists.txt", "sdkconfig.defaults", "README.md")) {
        $s = Join-Path $projDir $f
        if (Test-Path $s) { Copy-Item $s (Join-Path $BuildDir $f) -Force }
    }
    # sdkconfig 是生成物，存在就带过去（保留用户配置）
    $sc = Join-Path $projDir "sdkconfig"
    if (Test-Path $sc) { Copy-Item $sc (Join-Path $BuildDir "sdkconfig") -Force }

    Say "  同步完成" "Green"
}

$workDir = if ($needAscii) { $BuildDir } else { $projDir }
Set-Location $workDir

if ($Clean) {
    Step "清理"
    if (Test-Path "build")     { Remove-Item -Recurse -Force "build";     Say "  删除 build/" }
    if (Test-Path "sdkconfig") { Remove-Item -Force "sdkconfig";         Say "  删除 sdkconfig" }
}

# ---------------------------------------------------------------- 构建
Step "构建"
& $py $idfPy -DIDF_TARGET=esp32s3 build
if ($LASTEXITCODE -ne 0) { Say "构建失败 exit=$LASTEXITCODE" "Red"; exit $LASTEXITCODE }

$binPath = Join-Path $workDir "build\esp32_gimbal.bin"
if (-not (Test-Path $binPath)) { Say "构建声称成功但找不到产物: $binPath" "Red"; exit 1 }

$sz = (Get-Item $binPath).Length
Say ("`n构建成功  固件 {0:N0} bytes" -f $sz) "Green"
Say ("  产物: {0}" -f $binPath)

# 把产物拷回项目目录（方便存档/给别人）
$outDir = Join-Path $projDir "build"
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
Copy-Item $binPath (Join-Path $outDir "esp32_gimbal.bin") -Force
Say ("  已回拷到项目: {0}" -f (Join-Path $outDir "esp32_gimbal.bin"))

# ---------------------------------------------------------------- 自动找端口
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
            $vid = ""; $pid = ""
            $vm = [regex]::Match($dev.DeviceID, 'VID_([0-9A-Fa-f]{4})')
            $pm = [regex]::Match($dev.DeviceID, 'PID_([0-9A-Fa-f]{4})')
            if ($vm.Success) { $vid = $vm.Groups[1].Value.ToUpper() }
            if ($pm.Success) { $pid = $pm.Groups[1].Value.ToUpper() }
            # 303A = Espressif 原生USB；1A86 = CH340/CH343
            if ($vid -eq "303A" -or $vid -eq "1A86") {
                $cands += [pscustomobject]@{ Com = $com; Vid = $vid; Pid = $pid; Name = $dev.Name }
            }
        }
    } catch { }

    if ($cands.Count -eq 0) {
        Say "  没找到 ESP32（VID_303A 或 VID_1A86）。USB 线插了吗？" "Red"
        Say "  可用端口：" "Yellow"
        [System.IO.Ports.SerialPort]::GetPortNames() | ForEach-Object { Say "    $_" }
        exit 1
    }
    foreach ($c in $cands) { Say ("  {0}  VID_{1} PID_{2}  {3}" -f $c.Com, $c.Vid, $c.Pid, $c.Name) }
    if ($cands.Count -gt 1) {
        Say ("  找到 {0} 个候选，用第一个 {1}（要指定就加 -Port）" -f $cands.Count, $cands[0].Com) "Yellow"
    }
    $Port = $cands[0].Com
}

# ---------------------------------------------------------------- 烧写
if ($Flash) {
    Step "烧写到 $Port"
    & $py $idfPy -p $Port flash
    if ($LASTEXITCODE -ne 0) { Say "烧写失败 exit=$LASTEXITCODE" "Red"; exit $LASTEXITCODE }
    Say "`n烧写完成" "Green"
    Say "  验证：读启动日志应看到  BOOT gimbal v2 / READY ... panPin=14 tiltPin=15 usbIn=1"
    Say "  若无这行说明 app_main 没执行到 —— 查 ESP_ERROR_CHECK 是否 abort（见 TROUBLESHOOTING 第 50 条）"
}

if ($Monitor) {
    Step "串口监视（Ctrl+] 退出）"
    & $py $idfPy -p $Port monitor
}
