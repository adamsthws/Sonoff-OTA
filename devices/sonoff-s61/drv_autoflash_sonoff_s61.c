// The Sonoff S61 (BK7238) for the conversion tool's OpenBeken driver
// (tools/beken/openbeken/drv_autoflash.c): where its stock store and
// download slot sit, its wiring and its meter's calibration.

#include "../new_common.h"
#include "../new_cfg.h"
#include "../new_pins.h"
#include "../logging/logging.h"
#include "drv_autoflash.h"

// The S61's energy meter is a BL0937 (pulse CF/CF1). The stock factory
// calibration lives in EF40 `elecCalibCoef` (80 B): a u32 magic, a flag byte,
// then three 24-byte records in the order current, voltage, power, each
// starting with three int32 a, b, c. Reverse-engineered from the stock
// firmware's bl0937b driver (docs/beken-s61-emulation.md): it first forms the
// HLW8012-datasheet nominal value from the pulse frequency f (Hz),
//
//     I_nom = f * 1.1 * 1000 / 94638          (1 mOhm shunt)
//     V_nom = f * 1.1 * 2040 / 15397          (2040:1 divider)
//     P_nom = f * 1.21 * 2040 * 1000 / 1721506
//
// then applies the factory trim  x = a*x^2/1e7 + b*x/1e6 + c/1e6  (the same
// CoolKit quadratic as the S60). OpenBeken's BL0937 driver also measures f
// (it counts falling edges per second) and scales it as real = f * cal, so for
// the linear trim every plug we have seen uses (a = c = 0) the OpenBeken
// calibration is simply  cal = K_nom * b / 1e6.  A non-zero a or c has no
// OpenBeken equivalent; we then still use b and say so.
#define S61_K_CURRENT (1.1 * 1000.0 / 94638.0)
#define S61_K_VOLTAGE (1.1 * 2040.0 / 15397.0)
#define S61_K_POWER   (1.21 * 2040.0 * 1000.0 / 1721506.0)
#define S61_RATED_POWER_W 3840.0f   // 16 A plug (the OpenBeken default PowerMax is 3680)

typedef struct { int32_t a, b, c; } s61_cal_rec_t;

static int s61_read_stock_calibration(s61_cal_rec_t rec[3]) {
	uint8_t coef[80];
	int n = Autoflash_ReadStockBlob("elecCalibCoef", coef, sizeof(coef));
	if (n < 8 + 3 * 24)
		return 0;
	for (int i = 0; i < 3; i++)
		memcpy(&rec[i], coef + 8 + 24 * i, sizeof(s61_cal_rec_t));
	return 1;
}

static float s61_cal(const char *what, double k, const s61_cal_rec_t *r) {
	if (r->a || r->c)
		ADDLOG_WARN(LOG_FEATURE_CFG, "AutoflashImport: %s trim is quadratic (a=%d c=%d); using the linear term only",
					what, (int)r->a, (int)r->c);
	return (float)(k * (double)r->b / 1e6);
}

// Carry the factory meter calibration into OpenBeken (first boot only).
static int s61_apply_stock_calibration(void) {
	s61_cal_rec_t rec[3];
	if (!s61_read_stock_calibration(rec)) {
		ADDLOG_INFO(LOG_FEATURE_CFG, "AutoflashImport: no stock elecCalibCoef; OpenBeken's BL0937 defaults stay");
		return 0;
	}
	if (rec[0].b <= 0 || rec[1].b <= 0 || rec[2].b <= 0) {
		ADDLOG_WARN(LOG_FEATURE_CFG, "AutoflashImport: stock calibration looks invalid (b=%d/%d/%d); not applied",
					(int)rec[0].b, (int)rec[1].b, (int)rec[2].b);
		return 0;
	}
	float ci = s61_cal("current", S61_K_CURRENT, &rec[0]);
	float cv = s61_cal("voltage", S61_K_VOLTAGE, &rec[1]);
	float cp = s61_cal("power", S61_K_POWER, &rec[2]);
	CFG_SetPowerMeasurementCalibrationFloat(CFG_OBK_CURRENT, ci);
	CFG_SetPowerMeasurementCalibrationFloat(CFG_OBK_VOLTAGE, cv);
	CFG_SetPowerMeasurementCalibrationFloat(CFG_OBK_POWER, cp);
	CFG_SetPowerMeasurementCalibrationFloat(CFG_OBK_POWER_MAX, S61_RATED_POWER_W);
	ADDLOG_INFO(LOG_FEATURE_CFG, "AutoflashImport: stock BL0937 factory trim b=%d/%d/%d (I/V/P) -> OpenBeken cal "
				"I=%.6f V=%.6f P=%.6f, PowerMax %d W",
				(int)rec[0].b, (int)rec[1].b, (int)rec[2].b, ci, cv, cp, (int)S61_RATED_POWER_W);
	return 1;
}

// The S61's wiring, from the OpenBeken device database entry for the
// S61STPF / board WBBK_01P_V1.3 (verified on hardware by its author,
// elektroda.com topic 4157852), which agrees with the stock driver's own
// parameter block (SEL on P24).
static const autoflash_pin_t s61_pins[] = {
	{26, IOR_Relay, 1},
	{9, IOR_Button_ToggleAll, 1},
	{0, IOR_LED_n, 1},
	{1, IOR_LED_WIFI_n, 2},
	{8, IOR_BL0937_CF, 0},
	{6, IOR_BL0937_CF1, 0},
	{24, IOR_BL0937_SEL, 0},
};

const autoflash_device_t autoflash_device = {
	.kind = "sonoff-s61",
	.stock_model = "SN-BK7238-S61-01",
	.name = "S61",
	.store_start = 0x3e0000,
	.store_end = 0x3ec000,
	.download_slot = 0x286000,
	.settings_sector = 0x3e1000,
	.pins = s61_pins,
	.pin_count = sizeof(s61_pins) / sizeof(s61_pins[0]),
	.pins_described = "Rel P26, Btn P9, LED P0, WifiLED P1, BL0937 CF P8 CF1 P6 SEL P24",
	.relay_channel = 1,
	.apply_calibration = s61_apply_stock_calibration,
};
