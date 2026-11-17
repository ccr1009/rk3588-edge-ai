#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flash_and_run_diag.py — 烧写舵机诊断固件并跑完 4 个实验，自动记录结果

为什么要有这个脚本
------------------
诊断固件的价值在「电压 + 症状」对得上。人肉盯着串口抄一张表容易漏行，
所以这里让脚本把 T 命令的完整输出落盘，再自动解析成结论。

用法
----
    # 1. 自动找端口，烧写 + 跑实验（推荐）
    python flash_and_run_diag.py

    # 2. 指定端口
    python flash_and_run_diag.py --port COM6

    # 3. 已经烧过了，只跑实验不重烧
    python flash_and_run_diag.py --no-flash

    # 4. 只跑某一个实验（B/M/S/R 之一）
    python flash_and_run_diag.py --no-flash --exp S

环境
----
用 ESP-IDF 自带的 python（里面已经装好 esptool + pyserial）：
    C:\\Users\\15354\\.espressif\\python_env\\idf6.0_py3.13_env\\Scripts\\python.exe
"""
import argparse
import os
import re
import subprocess
import sys
import time

BAUD_FLASH = 460800
BAUD_RUN = 115200
REPO = os.path.dirname(os.path.abspath(__file__))
MERGED = os.path.join(REPO, "build", "servo_diag_merged.bin")


# ---------------------------------------------------------------- 找端口
def find_port():
    """按 VID 找 ESP32（303A 原生 USB / 1A86 CH340），找不到返回 None。"""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None

    cands = []
    for p in list_ports.comports():
        desc = "%s %s" % (p.description or "", p.hwid or "")
        if re.search(r"303A|1A86|CP210|CH34|Espressif", desc, re.I):
            cands.append(p.device)
    return cands[0] if cands else None


def list_all_ports():
    ports = []
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            ports.append("%s  %s" % (p.device, p.description))
    except Exception:
        pass
    return ports


# ---------------------------------------------------------------- 烧写
def do_flash(port):
    if not os.path.exists(MERGED):
        print("❌ 找不到合并镜像: %s" % MERGED)
        print("   先构建：pwsh -File build_diag.ps1")
        return False

    print("=" * 68)
    print(" 烧写 servo_diag  ->  %s" % port)
    print(" 镜像 %s (%.0f KB)" % (MERGED, os.path.getsize(MERGED) / 1024.0))
    print("=" * 68)

    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3",
           "--port", port, "-b", str(BAUD_FLASH),
           "write-flash", "0x0", MERGED]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("\n❌ 烧写失败 exit=%d" % r.returncode)
        print("   常见原因：")
        print("   ① 端口被占用（先关掉 idf.py monitor / 串口助手）")
        print("   ② ESP32 没进下载模式（按住 BOOT 再点一下 RST）")
        print("   ③ USB 线只供电不传数据")
        return False
    print("\n✅ 烧写完成")
    return True


# ---------------------------------------------------------------- 跑实验
EXP_CMD = {"B": "B", "M": "M", "S": "S", "R": "R", "T": "T"}


def run_experiments(port, exp, out_file):
    import serial

    print("\n" + "=" * 68)
    print(" 串口 %s @%d，发送 '%s' 跑实验" % (port, BAUD_RUN, exp))
    if exp == "T":
        print(" 全部 4 个实验约 90 秒，中途不要拔线")
    print("=" * 68)

    ser = serial.Serial(port, BAUD_RUN, timeout=0.5)
    ser.dtr = False
    ser.rts = False
    time.sleep(0.4)
    ser.reset_input_buffer()

    ser.write((exp + "\r\n").encode())
    ser.flush()

    lines = []
    t0 = time.time()
    limit = 240 if exp == "T" else 120
    idle_after = 0

    while time.time() - t0 < limit:
        raw = ser.readline()
        if not raw:
            idle_after += 1
            # T 跑完后会静止；连续 40 次（约 20s）没输出就认为结束
            if lines and idle_after > 40:
                break
            continue
        idle_after = 0
        try:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
        except Exception:
            continue
        if not line.strip():
            continue
        # 过滤 IDF 启动噪声
        if line.startswith("I (") or line.startswith("W (") or line.startswith("E ("):
            print("   [sys] " + line)
            continue
        
        print("   " + line)
        lines.append(line)

    ser.close()

    with open(out_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n📄 原始输出已保存: %s" % out_file)
    return lines


# ---------------------------------------------------------------- 判读
def analyze(lines):
    """从输出里提出电压数据，给出结论。"""
    text = "\n".join(lines)

    if "BOOT servo_diag" not in text:
        print("\n⚠️ 没看到 BOOT servo_diag 行")
        print("   固件可能没跑起来。检查：")
        print("   • 烧写是否真的成功（地址 0x0）")
        print("   • 串口是不是选对了（ESP32 原生 USB 或 CH343 转的 UART）")
        print("   • 按住 BOOT 再点 RST 后重启一次")

    vs = [float(x) for x in re.findall(r"(\d\.\d\d)\s*V\b", text)]
    if not vs:
        print("\n⚠️ 没有采到电压数据（ADC 可能没接）")
        print("   不影响看动作：如果电压列全是 '--'，就只靠肉眼判断动作。")
        return

    vmax, vmin = max(vs), min(vs)
    print("\n" + "=" * 68)
    print(" 电压统计：最高 %.2f V / 最低 %.2f V / 掉压 %.2f V（%d 个采样点）"
          % (vmax, vmin, vmax - vmin, len(vs)))
    print("=" * 68)

    drop = vmax - vmin
    if vmin < 4.0:
        verdict = "② 供电塌陷"
        detail = ("最低电压 %.2f V，已经低于舵机可靠工作下限（4.0~4.8V）。\n"
                  "   舵机内部会欠压复位 → 表现就是「抖一下又弹回去、嗡嗡响不转」。\n"
                  "   这是你这次症状最可能的原因。" % vmin)
    elif drop > 0.6:
        verdict = "② 供电偏弱（临界）"
        detail = ("掉压 %.2f V，超过 0.6V 说明电源内阻偏大/电流不够。\n"
                  "   还能转但力矩不足，堵转时会掉得更多。" % drop)
    else:
        verdict = "① 供电正常 → 问题在机械或舵机本体"
        detail = ("电压稳定在 %.2f~%.2f V（掉压仅 %.2f V），电源没问题。\n"
                  "   那么「嗡/抖但不转」就是机械堵转或齿轮扫齿。\n"
                  "   看实验 3/4 里哪个角度开始异常 → 那个角度就是堵转点。" % (vmin, vmax, drop))

    print("\n【初步判读】%s" % verdict)
    print("   " + detail)

    # 找电压塌陷对应的角度（扫行程/爬坡行格式：  " +42    3.87 V   ...  ← 电压塌陷"）
    bad = re.findall(r"([+-])\s*(\d+)\s+(\d\.\d\d)\s*V[^\n]*?(?:电压塌陷|瞬时欠压)", text)
    if bad:
        angles = sorted({int(sign + num) for sign, num, v in bad if float(v) < 4.3})
        if angles:
            print("\n   电压塌陷集中在这些角度: %s"
                  % ", ".join("%+d°" % a for a in angles))

    print("\n" + "-" * 68)
    print(" 完整判读表（把观察到的动作对上）：")
    print("   电压全程 ≥4.6V，某角度开始嗡/抖  → ① 机械堵转：调支架角度或缩限位")
    print("   电压掉到 <4.3V                   → ② 供电不够：换电源 / 并 470µF 电容")
    print("   电压正常、不堵转、电机响轴不转     → ③ 齿轮扫齿：换 MG90S")
    print("=" * 68)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="烧写并运行舵机诊断固件")
    ap.add_argument("--port", default=None, help="串口，如 COM6；默认自动检测")
    ap.add_argument("--no-flash", action="store_true", help="跳过烧写，直接跑实验")
    ap.add_argument("--exp", default="T", choices=["T", "B", "M", "S", "R"],
                    help="跑哪个实验：T=全部(默认) B=基线 M=小幅 S=扫行程 R=爬坡")
    ap.add_argument("--out", default=None, help="输出文件路径")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("❌ 没找到 ESP32 串口（VID 303A / 1A86）")
        print("   当前可用端口：")
        for p in list_all_ports():
            print("     " + p)
        if not list_all_ports():
            print("     （一个都没有 —— USB 线插了吗？）")
        return 1

    print("使用端口: %s" % port)

    if not args.no_flash:
        if not do_flash(port):
            return 1
        time.sleep(2.0)          # 等它重启起来
    elif not os.path.exists(MERGED):
        print("⚠️ 未找到镜像，但 --no-flash 已跳过烧写")

    out_file = args.out or os.path.join(REPO, "build", "diag_result_%s.txt" % args.exp)
    os.makedirs(os.path.dirname(out_file), exist_ok=True)

    lines = run_experiments(port, args.exp, out_file)
    if not lines:
        print("\n❌ 一个字节都没收到")
        print("   串口选错了？还是固件没跑？试试 --no-flash 手动在串口助手里发 ? 看帮助")
        return 1

    analyze(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
