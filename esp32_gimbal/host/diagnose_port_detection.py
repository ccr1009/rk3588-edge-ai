#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断 build.ps1 端口检测失败的原因

现象（2026-11-17）
-----------------
用户把 ESP32 插到 PC，设备管理器显示 COM5 是 ESP32（VID_303A&PID_1001），
但 build.ps1 的自动检测报「没找到 ESP32（VID_303A 或 VID_1A86）」。

排查发现：在普通 PowerShell 会话里跑同样的逻辑**能正确识别**，
说明是 build.ps1 运行时的上下文差异。

可能原因
--------
1. build.ps1 用 `pwsh -File` 启动，可能是 PowerShell 7 vs 5.1 的
   Get-CimInstance 行为差异
2. 脚本里 `$ErrorActionPreference = "Stop"` 让某个 CIM 查询提前抛异常，
   被 `catch { }` 吞掉，导致 $cands 为空
3. Win32_PnPEntity 在某些情况下返回的 Name 不含 "COM5"
   （比如 Name 是父设备名，子接口才有 COM 号）

验证方法
--------
本脚本对比三种查询方式，看哪种能拿到 ESP32：
  A. Get-CimInstance Win32_PnPEntity | Where Name -match 'COM'
  B. Get-CimInstance Win32_SerialPort（专门查串口）
  C. 注册表 HARDWARE\\DEVICEMAP\\SERIALCOMM
"""
import subprocess
import sys


def run_ps(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=60)
    return (r.stdout or "").strip(), (r.stderr or "").strip()


def main():
    print("=" * 76)
    print(" build.ps1 端口检测诊断")
    print("=" * 76)

    # ---- 方式 A：build.ps1 用的 ----
    print()
    print("▶ A. Get-CimInstance Win32_PnPEntity | Where Name -match 'COM'")
    print("-" * 76)
    out, err = run_ps(
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.Name -match 'COM\\d+' } | "
        "Select-Object Name,DeviceID | Format-List"
    )
    print(out if out else "(空)")
    if err:
        print("[stderr]", err[:300])

    # ---- 方式 B：专门的串口类 ----
    print()
    print("▶ B. Get-CimInstance Win32_SerialPort")
    print("-" * 76)
    out, err = run_ps(
        "Get-CimInstance Win32_SerialPort | "
        "Select-Object DeviceID,Name,PNPDeviceID | Format-List"
    )
    print(out if out else "(空 —— 这类查询在某些系统上不可用)")
    if err:
        print("[stderr]", err[:300])

    # ---- 方式 C：注册表 ----
    print()
    print("▶ C. 注册表 HARDWARE\\DEVICEMAP\\SERIALCOMM")
    print("-" * 76)
    out, err = run_ps(
        "Get-ItemProperty 'HKLM:\\HARDWARE\\DEVICEMAP\\SERIALCOMM' | "
        "Format-List"
    )
    print(out if out else "(空)")
    if err:
        print("[stderr]", err[:300])

    # ---- 方式 D：直接用 .NET ----
    print()
    print("▶ D. [System.IO.Ports.SerialPort]::GetPortNames()")
    print("-" * 76)
    out, err = run_ps("[System.IO.Ports.SerialPort]::GetPortNames()")
    print(out if out else "(空)")

    # ---- 对比 build.ps1 的完整逻辑 ----
    print()
    print("▶ 用 build.ps1 的原逻辑跑一遍（看 $cands 是否为空）")
    print("-" * 76)
    out, err = run_ps(r"""
$ErrorActionPreference = "Stop"
$cands = @()
try {
    $devs = Get-CimInstance Win32_PnPEntity -ErrorAction Stop |
            Where-Object { $_.Name -match 'COM\d+' }
    Write-Output "匹配设备数: $($devs.Count)"
    foreach ($dev in $devs) {
        $m = [regex]::Match($dev.Name, 'COM(\d+)')
        if (-not $m.Success) { continue }
        $com = "COM" + $m.Groups[1].Value
        $vm = [regex]::Match($dev.DeviceID, 'VID_([0-9A-Fa-f]{4})')
        $pm = [regex]::Match($dev.DeviceID, 'PID_([0-9A-Fa-f]{4})')
        $vid = if ($vm.Success) { $vm.Groups[1].Value.ToUpper() } else { "" }
        Write-Output "  $com  VID=$vid  DeviceID=$($dev.DeviceID)"
        if ($vid -eq "303A" -or $vid -eq "1A86") {
            $cands += $com
        }
    }
    Write-Output "候选数: $($cands.Count)"
} catch {
    Write-Output "异常被捕获: $($_.Exception.Message)"
}
""")
    print(out if out else "(空)")
    if err:
        print("[stderr]", err[:500])

    print()
    print("=" * 76)
    print(" 判读")
    print("=" * 76)
    print(" 如果 A 能列出设备且 VID=303A，但 build.ps1 报找不到：")
    print("   → 问题在 build.ps1 的执行上下文（可能是 -File 调用时的差异）")
    print(" 如果 A 是空的：")
    print("   → Win32_PnPEntity 查询本身有问题，改用 B 或 C")
    print()
    print(" 临时绕过方法（已验证有效）：")
    print("   直接指定端口，跳过自动检测：")
    print("     pwsh -File build.ps1 -Flash -Port COM5")
    print()
    print("   或者直接用 esptool：")
    print("     & \"C:\\Users\\15354\\.espressif\\python_env\\idf6.0_py3.13_env\\Scripts\\python.exe\" `")
    print("         -m esptool --chip esp32s3 --port COM5 -b 460800 `")
    print("         write_flash 0x0 build\\esp32_gimbal_333hz_merged.bin")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
