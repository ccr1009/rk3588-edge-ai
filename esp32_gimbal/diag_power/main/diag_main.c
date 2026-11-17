/* diag_main.c — 舵机「只抖不转」诊断固件（ESP32-S3 / ESP-IDF v6.0.1）
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │ 为什么需要这个固件                                                   │
 * └──────────────────────────────────────────────────────────────────────┘
 * 现象：GPIO14 那颗底部舵机「只抖一下 / 嗡嗡响但不转」。
 * 信号侧已经用 R 回环自测排除：pad 上实测 pw 1499/2165/832us、per 20000us，
 * 是标准舵机波形。所以剩下三种可能，症状一模一样：
 *
 *   ① 机械堵转   舵机已经顶到支架/底座的硬限位，想继续转 → 嗡但不走
 *   ② 供电塌陷   5V 带不动。SG90 空载 ~100mA，堵转 1.4A，USB 或小 DC 模块
 *                一给电流电压就掉到 3~4V，舵机内部欠压复位 → 抖一下又回到原位
 *   ③ 齿轮扫齿   电机转、输出轴不动（扫齿后会听到电机空转的嗡嗡声）
 *
 * 本固件按顺序跑 4 个实验，每个实验都能把上述三条里的一到两条排除掉：
 *
 *   实验 1  中位静止基线      —— 不动作时的 5V 电压是多少（基准）
 *   实验 2  小幅度往返 ±10°   —— 幅度小到一定不会堵转，只考验供电
 *   实验 3  全行程慢扫        —— 找到「一动就抖/嗡」的具体角度（堵转点）
 *   实验 4  爬坡逼近           —— 每 3° 停 1s 逼近，看哪个角度开始塌压
 *
 * 同时全程在采样 5V 母线的 ADC 值（见下方接线），把「电压」和「症状」对上，
 * 就能分清是供电还是机械。没有 ADC 接线也能跑，只是少一列电压。
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │ 5V 电压怎么量（可选，但强烈建议接）                                  │
 * └──────────────────────────────────────────────────────────────────────┘
 *   外部 5V 电源 OUT+ ──┬── 100kΩ ──┬── GPIO1 (ADC1_CH0)
 *                       │           │
 *                   舵机红线     47kΩ
 *                                   │
 *                               GND（与 ESP32 共地）
 *
 *   分压比 = 47/(100+47) = 0.3197
 *   5.0V → 1.599V，ADC 11dB 衰减量程约 3.1V，留足余量。
 *   ★ 绝对不要把 5V 直接接 GPIO1，会烧 ADC。
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │ 命令（串口 115200，UART0 或原生 USB 都可）                           │
 * └──────────────────────────────────────────────────────────────────────┘
 *   T        跑全部 4 个实验（推荐，约 90 秒）
 *   B        实验 1：中位静止基线（5 秒）
 *   M        实验 2：小幅度 ±10° 往返（不堵转，纯测供电）
 *   S        实验 3：全行程慢扫，-90 → +90，每 6° 停 0.8s
 *   R        实验 4：爬坡逼近，每 3° 停 1s，从 0 往 +90 再往 -90
 *   A <pin>  改舵机信号脚（默认 14）
 *   P <deg>  手动设角度（相对中位，限 ±90）
 *   H        回中
 *   V        只报当前 5V 电压
 *   ?        帮助
 *
 * 判读（跑完 T 看这张表）：
 *   电压全程 ≥4.6V，但某个角度开始嗡/抖   → ① 机械堵转，调支架或缩限位
 *   电压掉到 <4.3V（掉 0.6V 以上）        → ② 供电不够，换电源/加电容/独立供电
 *   电压正常、不堵转、电机嗡嗡但轴不转     → ③ 齿轮扫齿，换舵机
 */

#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "driver/ledc.h"
#include "driver/gpio.h"
#include "driver/usb_serial_jtag.h"
#include "esp_rom_gpio.h"
#include "soc/gpio_sig_map.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

/* ============ 舵机 PWM 参数（与主固件一致，方便对照）============ */
#define SERVO_PIN_DEFAULT   14
#define SERVO_LEDC_CH       LEDC_CHANNEL_0
#define SERVO_LEDC_TIMER    LEDC_TIMER_0
#define SERVO_LEDC_MODE     LEDC_LOW_SPEED_MODE

#define SERVO_FREQ_HZ       50
#define SERVO_RES_BITS      LEDC_TIMER_14_BIT
#define SERVO_RES_LEVELS    (1U << 14)
#define SERVO_PERIOD_US     20000
#define PULSE_MIN_US        500
#define PULSE_MID_US        1500
#define PULSE_MAX_US        2500
#define SERVO_RANGE_DEG     90.0f     /* ±90° = 500~2500us */

/* ============ 5V 母线采样（分压 100k / 47k）============
 * ADC1_CH0 = GPIO1。分压比 R_LOW/(R_HIGH+R_LOW)。 */
#define VBUS_ADC_CHANNEL    ADC_CHANNEL_0
#define VBUS_DIV_NUM        47.0f
#define VBUS_DIV_DEN        147.0f
#define VBUS_DIV_RATIO      (VBUS_DIV_DEN / VBUS_DIV_NUM)   /* ≈3.128 */

#define V_ADC_UNUSED        (-1.0f)     /* 没接 ADC 时的哨兵值 */

static int          g_pin = SERVO_PIN_DEFAULT;
static float        g_deg = 0.0f;
static adc_oneshot_unit_handle_t g_adc = NULL;
static adc_cali_handle_t         g_cali = NULL;
static bool                      g_adc_ok = false;

/* ============ 输出（双控制台：UART0 + 原生 USB）============ */
static void out(const char *fmt, ...)
{
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (n <= 0) return;
    if (n > (int)sizeof(buf) - 1) n = (int)sizeof(buf) - 1;
    fwrite(buf, 1, (size_t)n, stdout);
    fflush(stdout);
}

/* ============ 角度 → duty ============ */
static uint32_t us_to_duty(uint32_t us)
{
    return (uint32_t)(((uint64_t)us * SERVO_RES_LEVELS) / SERVO_PERIOD_US);
}

static void write_deg(float deg)
{
    if (deg >  SERVO_RANGE_DEG) deg =  SERVO_RANGE_DEG;
    if (deg < -SERVO_RANGE_DEG) deg = -SERVO_RANGE_DEG;
    g_deg = deg;

    float   norm = deg / SERVO_RANGE_DEG;                 /* -1 .. +1 */
    uint32_t half = us_to_duty((PULSE_MAX_US - PULSE_MIN_US) / 2 + PULSE_MID_US);
    uint32_t mid  = us_to_duty(PULSE_MID_US);
    int32_t  duty = (int32_t)mid + (int32_t)(norm * (float)((int32_t)half - (int32_t)mid));

    int32_t lo = (int32_t)us_to_duty(PULSE_MIN_US);
    int32_t hi = (int32_t)us_to_duty(PULSE_MAX_US);
    if (duty < lo) duty = lo;
    if (duty > hi) duty = hi;

    ledc_set_duty(SERVO_LEDC_MODE, SERVO_LEDC_CH, (uint32_t)duty);
    ledc_update_duty(SERVO_LEDC_MODE, SERVO_LEDC_CH);
}

/* ============ 5V 母线电压 ============ */
static float read_vbus(void)
{
    if (!g_adc_ok) return V_ADC_UNUSED;

    /* 连采 16 次取中位，避开 PWM 边沿耦合进来的尖峰 */
    int   raw[16];
    for (int i = 0; i < 16; i++) {
        int v = 0;
        if (adc_oneshot_read(g_adc, VBUS_ADC_CHANNEL, &v) != ESP_OK) return V_ADC_UNUSED;
        raw[i] = v;
    }
    for (int i = 1; i < 16; i++) {
        int k = raw[i], j = i - 1;
        while (j >= 0 && raw[j] > k) { raw[j + 1] = raw[j]; j--; }
        raw[j + 1] = k;
    }
    int med = (raw[7] + raw[8]) / 2;

    int mv = 0;
    if (g_cali) {
        if (adc_cali_raw_to_voltage(g_cali, med, &mv) != ESP_OK) return V_ADC_UNUSED;
    } else {
        mv = (med * 3100) / 4095;      /* 无校准时按 3.1V 满量程粗算 */
    }
    return (float)mv / 1000.0f * VBUS_DIV_RATIO;
}

static void vbus_init(void)
{
    adc_oneshot_unit_init_cfg_t ucfg = { .unit_id = ADC_UNIT_1 };
    if (adc_oneshot_new_unit(&ucfg, &g_adc) != ESP_OK) {
        out("# ADC 初始化失败，电压列将显示 --\n");
        return;
    }
    adc_oneshot_chan_cfg_t ccfg = {
        .atten    = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    if (adc_oneshot_config_channel(g_adc, VBUS_ADC_CHANNEL, &ccfg) != ESP_OK) {
        out("# ADC 通道配置失败，电压列将显示 --\n");
        return;
    }

    adc_cali_curve_fitting_config_t cal = {
        .unit_id  = ADC_UNIT_1,
        .atten    = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    if (adc_cali_create_scheme_curve_fitting(&cal, &g_cali) != ESP_OK) {
        g_cali = NULL;                 /* 没有校准也能跑，只是精度差些 */
    }
    g_adc_ok = true;
    out("# ADC 就绪：5V 母线采样在 GPIO1（100k/47k 分压）\n");
}

/* ============ 采样窗口：停住时看电压，并记录最低值 ============ */
typedef struct {
    float v_start;
    float v_min;
    float v_max;
    int   n;
} vwin_t;

static void vwin_begin(vwin_t *w)
{
    w->v_start = read_vbus();
    w->v_min   = 1e9f;
    w->v_max   = -1e9f;
    w->n       = 0;
}

static void vwin_sample(vwin_t *w, int count)
{
    for (int i = 0; i < count; i++) {
        float v = read_vbus();
        if (v == V_ADC_UNUSED) return;
        if (v < w->v_min) w->v_min = v;
        if (v > w->v_max) w->v_max = v;
        w->n++;
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

static void vwin_end(vwin_t *w, float *v_end, float *v_lo, float *v_hi)
{
    float e = read_vbus();
    *v_end = e;
    *v_lo  = (w->n > 0) ? w->v_min : V_ADC_UNUSED;
    *v_hi  = (w->n > 0) ? w->v_max : V_ADC_UNUSED;
}

static void vfmt(float v, char *buf, size_t n)
{
    if (v == V_ADC_UNUSED) snprintf(buf, n, "   -- ");
    else                   snprintf(buf, n, "%5.2f", v);
}

/* ============ 实验 1：中位静止基线 ============ */
static void exp_baseline(void)
{
    out("\n=== 实验 1：中位静止基线（不动作时的 5V 电压）===\n");
    write_deg(0.0f);
    vTaskDelay(pdMS_TO_TICKS(600));

    vwin_t w;
    vwin_begin(&w);
    vwin_sample(&w, 200);              /* 1 s */

    float e, lo, hi;
    vwin_end(&w, &e, &lo, &hi);
    char b[16], c[16], d[16];
    vfmt(e, b, sizeof b); vfmt(lo, c, sizeof c); vfmt(hi, d, sizeof d);

    out("  静止 V=%s V  (min=%s max=%s, %d 采样)\n", b, c, d, w.n);
    out("  ↑ 这是基准。下面每个实验都和它比。\n");
}

/* ============ 实验 2：小幅度 ±10° 往返（一定不堵转）============ */
static void exp_small(void)
{
    out("\n=== 实验 2：小幅度 ±10° 往返（幅度小到不可能堵转，纯考验供电）===\n");
    out("  序号  角度   停稳后V   窗口min   窗口max   与基线差\n");

    float base = read_vbus();
    char bb[16]; vfmt(base, bb, sizeof bb);
    out("  基线           %s\n", bb);

    const int seq[] = { 10, 0, -10, 0, 10, 0, -10, 0 };
    for (int i = 0; i < (int)(sizeof(seq) / sizeof(seq[0])); i++) {
        write_deg((float)seq[i]);
        vTaskDelay(pdMS_TO_TICKS(500));      /* 等它转到位 */

        vwin_t w;
        vwin_begin(&w);
        vwin_sample(&w, 120);                /* 0.6 s */

        float e, lo, hi;
        vwin_end(&w, &e, &lo, &hi);
        char b[16], c[16], d[16], f[16];
        vfmt(e, b, sizeof b); vfmt(lo, c, sizeof c); vfmt(hi, d, sizeof d);
        if (base != V_ADC_UNUSED && e != V_ADC_UNUSED) snprintf(f, sizeof f, "%+5.2f", e - base);
        else                                           snprintf(f, sizeof f, "   -- ");
        out("   %2d   %+4d   %s V   %s     %s     %s\n", i + 1, seq[i], b, c, d, f);
    }
    write_deg(0.0f);
}

/* ============ 实验 3：全行程慢扫，找堵转点 ============ */
static void exp_sweep(void)
{
    out("\n=== 实验 3：全行程慢扫 -90 → +90，每 6° 停 0.8s ===\n");
    out("  盯住舵机：哪个角度开始「嗡 / 抖 / 不动」，记下那个角度。\n");
    out("  角度   停稳V   窗口min   备注\n");

    for (int d = -90; d <= 90; d += 6) {
        write_deg((float)d);
        vTaskDelay(pdMS_TO_TICKS(400));

        vwin_t w;
        vwin_begin(&w);
        vwin_sample(&w, 80);                 /* 0.4 s */

        float e, lo, hi;
        vwin_end(&w, &e, &lo, &hi);
        char b[16], c[16];
        vfmt(e, b, sizeof b); vfmt(lo, c, sizeof c);

        const char *note = "";
        if (e != V_ADC_UNUSED && e < 4.3f)       note = "  ← 电压塌陷";
        else if (lo != V_ADC_UNUSED && lo < 4.0f) note = "  ← 瞬时欠压";
        out("  %+4d   %s V   %s   %s\n", d, b, c, note);
    }
    write_deg(0.0f);
}

/* ============ 实验 4：爬坡逼近（每 3° 停 1s）============ */
static void exp_ramp(void)
{
    out("\n=== 实验 4：爬坡逼近，每 3° 停 1s ===\n");
    out("  慢慢逼近限位：电压在哪一度开始掉、舵机在哪一度停住/开始嗡。\n");
    out("  方向   角度   停稳V     掉压幅度\n");

    float base = read_vbus();

    for (int dir = 0; dir < 2; dir++) {
        const char *name = (dir == 0) ? " +  " : " -  ";
        write_deg(0.0f);
        vTaskDelay(pdMS_TO_TICKS(400));

        for (int step = 3; step <= 90; step += 3) {
            float d = (dir == 0) ? (float)step : (float)(-step);
            write_deg(d);
            vTaskDelay(pdMS_TO_TICKS(1000));     /* 停满 1s，让它真的到 */

            float v = read_vbus();
            char b[16], c[16];
            vfmt(v, b, sizeof b);
            if (base != V_ADC_UNUSED && v != V_ADC_UNUSED) snprintf(c, sizeof c, "%+5.2f", v - base);
            else                                          snprintf(c, sizeof c, "   -- ");

            const char *note = "";
            if (v != V_ADC_UNUSED && v < 4.3f) note = "  ← 电压塌陷";

            out("  %s   %+4d   %s V   %s%s\n", name, (int)d, b, c, note);
            if (v != V_ADC_UNUSED && v < 3.8f) {
                out("  !! 电压 <3.8V，舵机内部很可能已经欠压复位。停止逼近，先解决供电。\n");
                write_deg(0.0f);
                return;
            }
        }
    }
    write_deg(0.0f);
}

/* ============ 汇总 ============ */
static void exp_all(void)
{
    out("\n################################################################\n");
    out("#  舵机诊断：GPIO%d，4 个实验约 90 秒\n", g_pin);
    out("#  看动作 + 看电压，把「机械堵转 / 供电塌陷 / 齿轮扫齿」分开\n");
    out("################################################################\n");
    exp_baseline();
    exp_small();
    exp_sweep();
    exp_ramp();
    write_deg(0.0f);

    out("\n################################################################\n");
    out("#  判读表\n");
    out("#   电压全程 ≥4.6V，某角度开始嗡/抖    → ① 机械堵转（调支架或缩限位）\n");
    out("#   电压掉到 <4.3V（比基线低 0.6V 以上）→ ② 供电不够（换电源/加 470uF 电容）\n");
    out("#   电压正常、不堵转、电机响但轴不转    → ③ 齿轮扫齿，换 MG90S\n");
    out("################################################################\n");
}

/* ============ 帮助 ============ */
static void help(void)
{
    out("\n舵机诊断固件  pin=GPIO%d  (当前角度 %+.0f°)\n", g_pin, g_deg);
    out("  T        跑全部 4 个实验（约 90s，推荐）\n");
    out("  B        实验1 中位静止基线\n");
    out("  M        实验2 小幅度 ±10° 往返（不堵转，纯测供电）\n");
    out("  S        实验3 全行程慢扫 -90→+90\n");
    out("  R        实验4 爬坡逼近（每 3° 停 1s）\n");
    out("  A <pin>  改信号脚（默认 14）\n");
    out("  P <deg>  手动设角度（±90）\n");
    out("  H        回中\n");
    out("  V        只报当前 5V 电压\n");
    out("  ?        本帮助\n");
}

/* ============ 改引脚（走 GPIO 矩阵，先设方向后接信号）============
 * 顺序问题见主固件 gimbal_main.c 的 route_ledc_to_gpio 注释：
 * gpio_set_direction 会把矩阵路由抹掉，所以必须先方向、后接信号。 */
static void route_servo(int pin)
{
    int sig = LEDC_LS_SIG_OUT0_IDX + SERVO_LEDC_CH;
    gpio_set_direction((gpio_num_t)pin, GPIO_MODE_OUTPUT);
    gpio_set_drive_capability((gpio_num_t)pin, GPIO_DRIVE_CAP_3);
    esp_rom_gpio_connect_out_signal((gpio_num_t)pin, sig, false, false);
}

static void servo_init(void)
{
    ledc_timer_config_t tcfg = {
        .speed_mode      = SERVO_LEDC_MODE,
        .timer_num       = SERVO_LEDC_TIMER,
        .duty_resolution = SERVO_RES_BITS,
        .freq_hz         = SERVO_FREQ_HZ,
        .clk_cfg         = LEDC_AUTO_CLK,
    };
    ESP_ERROR_CHECK(ledc_timer_config(&tcfg));

    ledc_channel_config_t ccfg = {
        .gpio_num   = g_pin,
        .speed_mode = SERVO_LEDC_MODE,
        .channel    = SERVO_LEDC_CH,
        .timer_sel  = SERVO_LEDC_TIMER,
        .duty       = 0,
        .hpoint     = 0,
    };
    ESP_ERROR_CHECK(ledc_channel_config(&ccfg));

    route_servo(g_pin);
    write_deg(0.0f);
}

/* ============ 命令处理 ============ */
static void handle_line(char *line)
{
    size_t len = strlen(line);
    while (len > 0 && (line[len - 1] == '\r' || line[len - 1] == ' ')) line[--len] = '\0';
    if (len == 0) return;

    char cmd = line[0];
    char *rest = line + 1;
    while (*rest == ' ') rest++;

    switch (cmd) {
    case 'T': case 't': exp_all();      break;
    case 'B': case 'b': exp_baseline(); break;
    case 'M': case 'm': exp_small();    break;
    case 'S': case 's': exp_sweep();    break;
    case 'R': case 'r': exp_ramp();     break;

    case 'A': case 'a': {
        int pin = atoi(rest);
        if (pin < 0 || pin > 48) { out("ERR bad_pin\n"); break; }
        /* 先把旧脚摘掉，再接到新脚，避免两个脚同时输出舵机信号 */
        esp_rom_gpio_connect_out_signal((gpio_num_t)g_pin, SIG_GPIO_OUT_IDX, false, false);
        g_pin = pin;
        route_servo(g_pin);
        write_deg(0.0f);
        out("OK pin=%d\n", g_pin);
        break;
    }

    case 'P': case 'p': {
        float d = strtof(rest, NULL);
        write_deg(d);
        out("OK %.1f\n", g_deg);
        break;
    }

    case 'H': case 'h':
        write_deg(0.0f);
        out("OK 0.0\n");
        break;

    case 'V': case 'v': {
        float v = read_vbus();
        char b[16];
        vfmt(v, b, sizeof b);
        out("VBUS %s V\n", b);
        break;
    }

    case '?': help(); break;

    default:
        out("ERR unknown_cmd (发 ? 看帮助)\n");
        break;
    }
}

static void diag_task(void *arg)
{
    (void)arg;
    char line[64];
    size_t pos = 0;

    servo_init();
    vbus_init();

    out("\nBOOT servo_diag v1\n");
    out("READY pin=%d adc=%d range=+-90deg\n", g_pin, g_adc_ok ? 1 : 0);
    out("发 ? 看帮助，T 跑全部实验\n\n");

    while (1) {
        int n;
        while ((n = getchar()) != EOF) {
            char c = (char)n;
            if (c == '\n' || c == '\r') {
                if (pos > 0) { line[pos] = '\0'; handle_line(line); pos = 0; }
            } else if (pos < sizeof(line) - 1) {
                line[pos++] = c;
            } else {
                pos = 0;
            }
        }

        char ubuf[32];
        int got = usb_serial_jtag_read_bytes(ubuf, sizeof(ubuf), 0);
        for (int i = 0; i < got; i++) {
            char c = ubuf[i];
            if (c == '\n' || c == '\r') {
                if (pos > 0) { line[pos] = '\0'; handle_line(line); pos = 0; }
            } else if (pos < sizeof(line) - 1) {
                line[pos++] = c;
            } else {
                pos = 0;
            }
        }

        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void app_main(void)
{
    usb_serial_jtag_driver_config_t usb_cfg = {
        .tx_buffer_size = 512,
        .rx_buffer_size = 512,
    };
    usb_serial_jtag_driver_install(&usb_cfg);

    xTaskCreate(diag_task, "diag", 8192, NULL, 5, NULL);
}
