#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_diag_from_board.py — 在 RK3588 上跑诊断固件（ESP32 插在板子上时用这条）

背景
----
ESP32 现在插在 RK3588 上，PC 上没有 COM 口。想烧新固件有两种走法：

  【A】把 ESP32 拔下来插 PC（最简单，推荐）
       PC 上直接跑： python host/flash_and_run_diag.py

  【B】ESP32 留在板子上，走 pyusb 烧（麻烦但不用动线）
       本脚本负责 B 的「跑实验」那半段；烧写见 --help 里的说明。

为什么 B 要单独处理
------------------
EVM3588-B 内核没编 CONFIG_USB_ACM，ESP32 的 CDC 建不出 /dev/ttyACM*。
所以板端只能用 pyusb 在用户态直接读写 bulk 端点（已有 usb_cdc_serial.py）。
esptool 烧写还需要精确复位时序，风险高（早期探针脚本 2026-11-17 已清理，
那是「能试但要接受失败」的方案。真烧不进去就拔到 PC 上烧。

用法（板子上）
------------
    # 跑全部实验
    /root/rknn_env/bin/python /root/esp32_gimbal/run_diag_from_board.py

    # 只跑某一个：B 基线 / M 小幅 / S 扫行程 / R 爬坡
    /root/rknn_env/bin/python /root/esp32_gimbal/run_diag_from_board.py S
"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial


def drain(ser, secs, stop_marker=None):
    """收 N 秒，把行打出来；遇到 stop_marker 提前结束。"""
    out = []
    t0 = time.time()
    while time.time() - t0 < secs:
        line = ser.readline()
        if not line:
            continue
        v = line.decode("utf-8", "replace").rstrip("\r\n")
        if not v.strip():
            continue
        print(v)
        sys.stdout.flush()
        out.append(v)
        if stop_marker and stop_marker in v:
            break
    return out


def main():
    exp = (sys.argv[1] if len(sys.argv) > 1 else "T").upper()
    if exp not in ("T", "B", "M", "S", "R", "V", "?"):
        print("用法: run_diag_from_board.py [T|B|M|S|R|V|?]")
        return 2

    print("=" * 66)
    print(" 连接 ESP32 ...")
    try:
        ser = UsbCdcSerial(timeout=0.5)
    except Exception as e:
        print("❌ 打不开 USB CDC: %s" % e)
        print("   • ESP32 插在板子上了吗？ lsusb 应看到 303a:1001")
        print("   • 桥接服务占着设备？ bash /root/esp32_gimbal/stop_bridge.sh")
        return 1

    ser.reset_input_buffer()
    time.sleep(0.3)
    ser.reset_input_buffer()

    # 先探活：诊断固件的帮助输出里有 "servo_diag"
    ser.write(b"?\r\n")
    ser.flush()
    time.sleep(1.0)
    probe = drain(ser, 1.5)

    if not any("servo_diag" in v for v in probe):
        print()
        print("⚠️ 没看到 servo_diag 的帮助信息 —— 当前的固件还不是诊断固件。")
        print("   先烧 servo_diag（二选一）：")
        print("   【A 推荐】把 ESP32 拔下来插 PC，在 PC 上跑：")
        print("             python host/flash_and_run_diag.py")
        print("   【B】留在板子上走 pyusb 烧（成功率低）：")
        print("        bash /root/esp32_gimbal/stop_bridge.sh")
        print("        python3 /root/esp32_gimbal/esptool_over_usb.py   # 另开一个终端")
        print("        # 然后按它打印的 pty 路径跑 esptool write-flash 0x0 <merged.bin>")
        print()
        print("   当前收到的内容:")
        for v in probe[:10]:
            print("     " + v)
        ser.close()
        return 1

    print()
    print("=" * 66)
    print(" 发送 '%s' 开跑" % exp)
    print("=" * 66)

    ser.write((exp + "\r\n").encode())
    ser.flush()

    # T 全跑约 90s，给足余量
    limit = 200 if exp == "T" else 90
    lines = drain(ser, limit)
    ser.close()

    print()
    print("=" * 66)
    print(" 完成，共 %d 行输出" % len(lines))
    # 顺手把关键结论再提一遍
    import re
    vs = [float(x) for x in re.findall(r"(\d\.\d\d)\s*V\b", "\n".join(lines))]
    if vs:
        print(" 电压：最高 %.2f V / 最低 %.2f V / 掉压 %.2f V" % (max(vs), min(vs), max(vs) - min(vs)))
        if min(vs) < 4.0:
            print(" → ② 供电塌陷是主因：换电源 / 并 470µF 电容")
        elif max(vs) - min(vs) > 0.6:
            print(" → ② 供电偏弱：电源内阻大或电流不够")
        else:
            print(" → ① 供电正常：问题在机械堵转或齿轮扫齿")
    else:
        print(" 没采到电压（ADC 可能没接），只能靠肉眼判断动作")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
