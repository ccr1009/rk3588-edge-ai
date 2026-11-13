#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""definitive_test.py — 接线已验证后的决定性动作测试

背景：
    2026-11-06 用万用表确认了三件事（这是以前从没做到过的）：
        测1  舵机红/棕线之间 = 4.8~5.2V      → 电源到了
        测2  电源 OUT- 与 ESP32 GND 通断 = 响 → 共地通了
        测4  舵机橙线对 ESP32 GND = 0.25V     → PWM 信号到舵机端了
    但同时：摇臂很松，没有自锁力。

    这三种可能还没分开：
        A. 舵机烧了（内部驱动电路坏了）
        B. 这是**连续旋转舵机**（360° 那种），1500us 时本来就停转、
           没有自锁力 —— 症状和"坏了"一模一样
        C. 还有别的接线问题（但三根线都量过了，可能性低）

    这个脚本用「动作特征」来分开 A 和 B：
        定位舵机      → 给 1000us / 2000us 会**转到某个角度停住**，
                        给 1500us 会回到中位并**自锁**
        连续旋转舵机  → 给 1000us / 2000us 会**朝两个方向持续旋转**，
                        给 1500us 会停转（无自锁）

跑法：
    bash /root/esp32_gimbal/stop_bridge.sh
    /root/rknn_env/bin/python /root/esp32_gimbal/definitive_test.py
"""
import sys
import time

sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial


def drain(ser, dur):
    out = []
    t0 = time.time()
    while time.time() - t0 < dur:
        line = ser.readline()
        if not line:
            continue
        v = line.decode("utf-8", "replace").strip()
        if v:
            out.append(v)
    return out


def one(ser, cmd, wait=0.6):
    ser.write((cmd + "\n").encode())
    ser.flush()
    t0 = time.time()
    while time.time() - t0 < wait:
        line = ser.readline()
        if line:
            v = line.decode("utf-8", "replace").strip()
            if v:
                return v
    return None


def say(txt=""):
    print(txt)
    sys.stdout.flush()


def wait_secs(n, note=""):
    for i in range(n, 0, -1):
        sys.stdout.write("\r      倒数 %d ...  %s            " % (i, note))
        sys.stdout.flush()
        time.sleep(1.0)
    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()


def main():
    say("=" * 72)
    say("决定性动作测试 —— 分清「舵机坏了」和「连续旋转舵机」")
    say("=" * 72)
    say()
    say("接线已验证：5V ✅  共地 ✅  PWM 0.25V ✅")
    say("所以这个测试的结果是可信的。")
    say()
    say("★ 请把注意力放在舵机摇臂上，看它【怎么动】，不只是看【动没动】★")
    say()

    try:
        ser = UsbCdcSerial(timeout=0.4)
    except Exception as e:
        say("打不开 ESP32: %s" % e)
        say("先停桥： bash /root/esp32_gimbal/stop_bridge.sh")
        return 1

    ser.reset_input_buffer()
    r = one(ser, "I", 1.0)
    say("固件: %s" % r)
    if not r or not r.startswith("GPIN"):
        say("通信异常，先解决这个")
        return 1

    # ★★★ 这里踩过一个坑，务必注意 ★★★
    # 第一版写的是 for c in ["D 0", "A", "X", "P 0 0"]，以为 X 也是"重置"。
    # 但固件里 'X' = start_scan()，是**启动引脚扫描**：
    #     update_scan() 每 1.2s 把 g_pan_pin 换一个（14 → 15 → 16 → ... → 47）
    # 而主循环里扫描优先级最高（else if (g_scanning) update_scan();），
    # 扫描期间 P 命令设的角度**完全不生效** —— 因为 PWM 被 update_scan 覆盖了。
    # 结果：测试全程 PWM 发在错误的引脚上，舵机不动，还读到 GPIN 40 15。
    #
    # 现在只发真正"停止"类命令：
    #   D 0  = 停演示摆动
    #   A    = 停 GPIO 翻转
    # **不发 X**。如果扫描已经在跑（比如上一次误发），只能等它自己跑完
    # （SCAN_PIN_COUNT=24 个脚 × 1.2s ≈ 29 秒），或者复位板子。
    for c in ["D 0", "A"]:
        one(ser, c, 0.4)
    time.sleep(0.5)

    # 扫完确认引脚确实回到 14，否则等它跑完
    for attempt in range(3):
        r = one(ser, "I", 1.0)
        if r and "GPIN 14" in r:
            say("引脚确认: %s" % r)
            break
        say("引脚不对 (%s)，等扫描跑完..." % r)
        time.sleep(12)

    one(ser, "P 0 0", 0.5)
    time.sleep(0.4)
    ser.reset_input_buffer()

    say()
    say("=" * 72)
    say("测试 1 / 3：慢速扫到底 —— 定位舵机会「转到角度停住」")
    say("=" * 72)
    say("  连续旋转舵机会「转一小段然后停」，或者干脆不转。")
    say("  观察要点：停下来的时候，摇臂是停在【某个固定角度】吗？")
    say()
    wait_secs(4, "手离开舵机，盯住摇臂")

    # 慢速走三个位置，每个位置停够久
    for label, pan in [("最左 (-45° = 1000us)", -45), ("最右 (+45° = 2000us)", 45), ("回到中位 (1500us)", 0)]:
        say()
        say("    >>> 现在发 P %d 0   %s" % (pan, label))
        one(ser, "P %d 0" % pan, 0.3)
        say("        保持 4 秒 —— 看它是不是停在固定位置")
        drain(ser, 4.0)

    say()
    say("=" * 72)
    say("测试 2 / 3：快速左右交替 —— 定位会摆，连续旋转会持续转")
    say("=" * 72)
    say("  交替 1000us / 2000us，各 1 秒，重复 6 次。")
    say("  · 定位舵机     → 看到来回摆动（左→右→左→右）")
    say("  · 连续旋转舵机 → 看到【单向持续旋转】，方向随指令翻转")
    say("  · 完全不动     → 舵机坏了")
    say()
    wait_secs(3, "准备观察")

    for i in range(6):
        pan = -45 if i % 2 == 0 else 45
        one(ser, "P %d 0" % pan, 0.2)
        say("      第 %d 次 -> P %+d 0  (%s)" % (i + 1, pan, "1000us" if pan < 0 else "2000us"))
        drain(ser, 1.0)

    # 回中
    one(ser, "P 0 0", 0.5)
    drain(ser, 1.0)

    say()
    say("=" * 72)
    say("测试 3 / 3：中位保持 6 秒 —— 测自锁力")
    say("=" * 72)
    say("  现在舵机停在 1500us（中位）。")
    say("  ★ 请用手轻轻拨摇臂 ★")
    say()
    say("  · 定位舵机     → 捏不动，或松手会弹回中位")
    say("  · 连续旋转舵机 → 随便就能转动（因为中位=停止，本来就没力矩）")
    say("  · 舵机坏了     → 随便就能转动")
    say()
    wait_secs(6, "用手拨摇臂试试")

    one(ser, "P 0 0", 0.5)
    ser.close()

    say()
    say("=" * 72)
    say("请回答：你刚才看到了什么？")
    say("=" * 72)
    say()
    say("  ① 慢扫时「转到某个角度就停住不动了」")
    say("     + 交替时「来回摆」")
    say("     + 中位「捏不动」")
    say("       → ✅ 定位舵机，工作完全正常 —— 那问题在别处（告诉我，我继续查）")
    say()
    say("  ② 慢扫时「转一小段就没劲了」或「一直单向转不停」")
    say("     + 交替时「单向持续旋转，方向会翻」")
    say("     + 中位「随便转」")
    say("       → ⚠️ 这是【360° 连续旋转舵机】，不是定位舵机！")
    say("          它没坏，但**不能用于云台定位**，必须换成定位舵机")
    say()
    say("  ③ 全程「完全不动、没声音、摇臂松」")
    say("       → ❌ 舵机内部损坏（5V/地/PWM 都验证到位了还不转）")
    say("          换一个舵机试；换完还不动再回来找我")
    say()
    say("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
