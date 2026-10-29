// Sonoff S61 (BK7238) stock Wi-Fi credential import for OpenBeken.
//
// When a stock Sonoff S61 is converted to OpenBeken over the air, the plug's
// Wi-Fi SSID and password are still sitting in the stock EasyFlash V4 (EF40)
// key-value store, which the conversion does not touch (it lives well above the
// app partition the OTA replaces). This module reads them on first boot and
// hands them to OpenBeken's own config, so the converted plug rejoins the
// user's network with no typing and no second device.
//
// It deliberately goes through CFG_SetWiFiSSID() / CFG_SetWiFiPass() rather than
// writing the config blob itself: those setters own mainConfig_t's layout and
// checksum, so this keeps working across OpenBeken releases that change the
// config structure. The only external shapes it depends on are the stock EF40
// node format (frozen in Sonoff's firmware) and the BK7231/BK7238 flash-read
// primitive.
//
// Reversible by design: it reads the stock store, never writes it.
//
// Trigger it from the conversion (idempotent; a no-op once Wi-Fi is set):
//     startDriver SonoffS61Import
// or call Sonoff_S61_ImportStockWiFi() once from platform init.

#include "../new_common.h"
#include "../new_cfg.h"
#include "../new_pins.h"
#include "../logging/logging.h"
#include "../cmnds/cmd_public.h"
#include "../hal/hal_ota.h"

// Raw flash read, BK7231/BK7238 HAL (hal/bk7231/hal_flashConfig_bk7231.c).
extern int beken_hal_flash_read(const unsigned int addr, void *dst, const unsigned int size);

// EF40 "app" key-value store: physical range and the keys we want. Values are
// stored verbatim (plaintext) on a provisioned plug; see docs/beken-s61-emulation.md.
#define EF40_APP_START      0x3e0000
#define EF40_APP_END        0x3ec000
#define EF40_SCAN_CHUNK     0x1000      // 4 KiB read window
#define EF40_SCAN_OVERLAP   0x80        // keep a node from straddling two windows
#define EF40_MAX_VALUE      128

// One node: "KV40" | len u32 | crc32 u32 | name_len u8 + 0xff*3 | value_len u32
//           | name | value | trailer. crc32 (zlib/reflected, poly 0xEDB88320)
// covers the bytes from the name_len field through the end of the value.
#define KV40_HDR            20
#define KV40_OFS_NAMELEN    12
#define KV40_OFS_VALUELEN   16

static uint32_t ef40_crc32(const uint8_t *p, int len) {
	uint32_t crc = 0xffffffff;
	for (int i = 0; i < len; i++) {
		crc ^= p[i];
		for (int b = 0; b < 8; b++)
			crc = (crc & 1) ? (crc >> 1) ^ 0xEDB88320 : (crc >> 1);
	}
	return ~crc;
}

// Find the last valid value for `key` in `buf` (a window of the store). Returns
// the value length (<= outMax-1, NUL-terminated into out) or -1 if not present.
static int ef40_find_in_window(const uint8_t *buf, int buflen,
							   const char *key, char *out, int outMax) {
	int keyLen = (int)strlen(key);
	int found = -1;
	for (int i = 0; i + KV40_HDR <= buflen - 4; i++) {
		if (buf[i] != 'K' || buf[i+1] != 'V' || buf[i+2] != '4' || buf[i+3] != '0')
			continue;
		int nameLen = buf[i + KV40_OFS_NAMELEN];
		uint32_t valueLen;
		memcpy(&valueLen, buf + i + KV40_OFS_VALUELEN, 4);
		if (nameLen != keyLen || valueLen == 0 || valueLen >= EF40_MAX_VALUE)
			continue;
		int nameOfs = i + KV40_HDR;
		int valueOfs = nameOfs + nameLen;
		if (valueOfs + (int)valueLen > buflen)
			continue;                         // straddles the window; next window has it
		if (memcmp(buf + nameOfs, key, keyLen) != 0)
			continue;
		uint32_t stored;
		memcpy(&stored, buf + i + 8, 4);
		if (ef40_crc32(buf + i + KV40_OFS_NAMELEN,
					   (valueOfs + valueLen) - (i + KV40_OFS_NAMELEN)) != stored)
			continue;
		int n = (int)valueLen;
		if (n > outMax - 1) n = outMax - 1;
		memcpy(out, buf + valueOfs, n);
		out[n] = 0;
		found = n;                            // keep scanning: last write wins
	}
	return found;
}

// Read the EF40 app store in overlapping windows and return the newest value for
// `key`. Returns length or -1. Scanning windows keeps RAM use to one chunk.
static int ef40_read_key(const char *key, char *out, int outMax) {
	static uint8_t win[EF40_SCAN_CHUNK];
	int best = -1;
	char tmp[EF40_MAX_VALUE];
	for (uint32_t addr = EF40_APP_START; addr < EF40_APP_END;
		 addr += EF40_SCAN_CHUNK - EF40_SCAN_OVERLAP) {
		uint32_t want = EF40_SCAN_CHUNK;
		if (addr + want > EF40_APP_END) want = EF40_APP_END - addr;
		if (beken_hal_flash_read(addr, win, want) != 0)
			continue;
		int n = ef40_find_in_window(win, (int)want, key, tmp, sizeof(tmp));
		if (n >= 0) {                         // a later window holds a newer write
			best = n;
			if (n > outMax - 1) n = outMax - 1;
			memcpy(out, tmp, n);
			out[n] = 0;
		}
	}
	return best;
}

// The import itself. Safe to call repeatedly: it only acts when OpenBeken has no
// Wi-Fi SSID yet, so a provisioned device is never disturbed.
int Sonoff_S61_ImportStockWiFi(void) {
	char ssid[EF40_MAX_VALUE];
	char pass[EF40_MAX_VALUE];

	if (CFG_GetWiFiSSID()[0] != 0) {
		ADDLOG_INFO(LOG_FEATURE_CFG, "S61Import: Wi-Fi already set, skipping");
		return 0;
	}
	if (ef40_read_key("wifi-ssid", ssid, sizeof(ssid)) <= 0) {
		ADDLOG_INFO(LOG_FEATURE_CFG, "S61Import: no stock wifi-ssid in EF40 (unprovisioned?)");
		return 0;
	}
	if (ef40_read_key("wifi-pass", pass, sizeof(pass)) < 0)
		pass[0] = 0;                          // open network: SSID without a pass is valid

	CFG_SetWiFiSSID(ssid);
	CFG_SetWiFiPass(pass);
	CFG_Save_SetupTimer();                    // persist shortly; Wi-Fi joins on next connect
	ADDLOG_INFO(LOG_FEATURE_CFG, "S61Import: imported stock Wi-Fi SSID '%s' (%d-char pass)",
				ssid, (int)strlen(pass));
	return 1;
}

// Read a binary EF40 value (e.g. elecCalibCoef) into out, returns its length or -1.
static int ef40_read_blob(const char *key, uint8_t *out, int outMax) {
	static uint8_t win[EF40_SCAN_CHUNK];
	char tmp[EF40_MAX_VALUE];
	int best = -1;
	int keyLen = (int)strlen(key);
	for (uint32_t addr = EF40_APP_START; addr < EF40_APP_END;
		 addr += EF40_SCAN_CHUNK - EF40_SCAN_OVERLAP) {
		uint32_t want = EF40_SCAN_CHUNK;
		if (addr + want > EF40_APP_END) want = EF40_APP_END - addr;
		if (beken_hal_flash_read(addr, win, want) != 0)
			continue;
		// inline scan that keeps the raw value length (ef40_find_in_window
		// NUL-terminates, which is wrong for binary blobs with embedded zeros)
		for (int i = 0; i + KV40_HDR <= (int)want - 4; i++) {
			if (win[i] != 'K' || win[i+1] != 'V' || win[i+2] != '4' || win[i+3] != '0')
				continue;
			int nameLen = win[i + KV40_OFS_NAMELEN];
			uint32_t valueLen;
			memcpy(&valueLen, win + i + KV40_OFS_VALUELEN, 4);
			if (nameLen != keyLen || valueLen == 0 || valueLen >= EF40_MAX_VALUE)
				continue;
			int nameOfs = i + KV40_HDR, valueOfs = nameOfs + nameLen;
			if (valueOfs + (int)valueLen > (int)want)
				continue;
			if (memcmp(win + nameOfs, key, keyLen) != 0)
				continue;
			uint32_t stored;
			memcpy(&stored, win + i + 8, 4);
			if (ef40_crc32(win + i + KV40_OFS_NAMELEN,
						   (valueOfs + valueLen) - (i + KV40_OFS_NAMELEN)) != stored)
				continue;
			int n = (int)valueLen;
			if (n > outMax) n = outMax;
			memcpy(out, win + valueOfs, n);
			best = n;
			(void)tmp;
		}
	}
	return best;
}

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

int Sonoff_S61_ReadStockCalibration(s61_cal_rec_t rec[3]) {
	uint8_t coef[80];
	int n = ef40_read_blob("elecCalibCoef", coef, sizeof(coef));
	if (n < 8 + 3 * 24)
		return 0;
	for (int i = 0; i < 3; i++)
		memcpy(&rec[i], coef + 8 + 24 * i, sizeof(s61_cal_rec_t));
	return 1;
}

static float s61_cal(const char *what, double k, const s61_cal_rec_t *r) {
	if (r->a || r->c)
		ADDLOG_WARN(LOG_FEATURE_CFG, "S61Import: %s trim is quadratic (a=%d c=%d); using the linear term only",
					what, (int)r->a, (int)r->c);
	return (float)(k * (double)r->b / 1e6);
}

// Carry the factory meter calibration into OpenBeken (first boot only).
int Sonoff_S61_ApplyStockCalibration(void) {
	s61_cal_rec_t rec[3];
	if (!Sonoff_S61_ReadStockCalibration(rec)) {
		ADDLOG_INFO(LOG_FEATURE_CFG, "S61Import: no stock elecCalibCoef; OpenBeken's BL0937 defaults stay");
		return 0;
	}
	if (rec[0].b <= 0 || rec[1].b <= 0 || rec[2].b <= 0) {
		ADDLOG_WARN(LOG_FEATURE_CFG, "S61Import: stock calibration looks invalid (b=%d/%d/%d); not applied",
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
	ADDLOG_INFO(LOG_FEATURE_CFG, "S61Import: stock BL0937 factory trim b=%d/%d/%d (I/V/P) -> OpenBeken cal "
				"I=%.6f V=%.6f P=%.6f, PowerMax %d W",
				(int)rec[0].b, (int)rec[1].b, (int)rec[2].b, ci, cv, cp, (int)S61_RATED_POWER_W);
	return 1;
}

// The S61's wiring, from the OpenBeken device database entry for the
// S61STPF / board WBBK_01P_V1.3 (verified on hardware by its author,
// elektroda.com topic 4157852), which agrees with the stock driver's own
// parameter block (SEL on P24). Applied on first boot so the converted plug
// switches, meters and lights up straight away; the relay remembers its last
// state across power loss, as the tool sets on the S60.
static void Sonoff_S61_ApplyPinTemplate(void) {
	PIN_SetPinRoleForPinIndex(26, IOR_Relay);            PIN_SetPinChannelForPinIndex(26, 1);
	PIN_SetPinRoleForPinIndex(9, IOR_Button_ToggleAll);  PIN_SetPinChannelForPinIndex(9, 1);
	PIN_SetPinRoleForPinIndex(0, IOR_LED_n);             PIN_SetPinChannelForPinIndex(0, 1);
	PIN_SetPinRoleForPinIndex(1, IOR_LED_WIFI_n);        PIN_SetPinChannelForPinIndex(1, 2);
	PIN_SetPinRoleForPinIndex(8, IOR_BL0937_CF);
	PIN_SetPinRoleForPinIndex(6, IOR_BL0937_CF1);
	PIN_SetPinRoleForPinIndex(24, IOR_BL0937_SEL);
	CFG_SetChannelStartupValue(1, -1);                   // -1: restore the last relay state
	CFG_SetShortDeviceName("S61");
	ADDLOG_INFO(LOG_FEATURE_CFG, "S61Import: applied the S61 pin template (Rel P26, Btn P9, LED P0, WifiLED P1, "
				"BL0937 CF P8 CF1 P6 SEL P24); relay restores its last state");
}

// First boot after conversion: creds, then - only when they were just imported,
// so a user's later changes are never overwritten - the meter calibration and
// the S61 wiring.
static int Sonoff_S61_FirstBoot(void) {
	int imported = Sonoff_S61_ImportStockWiFi();
	if (imported > 0) {
		Sonoff_S61_ApplyStockCalibration();
		Sonoff_S61_ApplyPinTemplate();
	}
	return imported;
}

static commandResult_t Cmd_S61Import(const void *context, const char *cmd,
									const char *args, int cmdFlags) {
	return Sonoff_S61_FirstBoot() >= 0 ? CMD_RES_OK : CMD_RES_ERROR;
}

// Revert to stock: stage a stock-app rt_ota RBL where the STOCK bootloader looks
// for an update, then let it apply on reboot. On a converted S61 the stock
// bootloader is still in place and reads the download slot at 0x286000. The SDK
// picks its partition table from the flash size at runtime; on the S61's 4 MB
// chip that is the 4M table, whose OTA partition is exactly 0x286000 - the same
// Beken SDK map Sonoff built on - so OpenBeken's ordinary OTA download lands in
// the right slot. We check that before writing anything: on a 2 MB table the
// slot would be 0x132000, inside the app, and the stock bootloader would never
// see it. The stock EasyFlash store is untouched throughout, so the plug comes
// back with its own Wi-Fi, calibration and identity.
//     S61RevertToStock http://<tool>/stock_app_1.0.2.rbl
#include "BkDriverFlash.h"
#define S61_STOCK_DOWNLOAD_SLOT 0x286000

static commandResult_t Cmd_S61RevertToStock(const void *context, const char *cmd,
										   const char *args, int cmdFlags) {
	bk_logic_partition_t *pt = bk_flash_get_info(BK_PARTITION_OTA);
	if (args == NULL || args[0] == 0) {
		ADDLOG_ERROR(LOG_FEATURE_CFG, "S61Revert: give the URL of the stock app .rbl");
		return CMD_RES_NOT_ENOUGH_ARGUMENTS;
	}
	// Two addresses must agree: the SDK partition table (what the flash size
	// selected) and START_ADR_OF_BK_PARTITION_OTA, the constant OpenBeken's own
	// OTA downloader actually writes to (hal_ota.h; the stock build says
	// 0x132000 for BK7238). A build with either wrong would put the stock image
	// where the stock bootloader never looks, so refuse rather than guess.
	if (pt == NULL || pt->partition_start_addr != S61_STOCK_DOWNLOAD_SLOT
		|| START_ADR_OF_BK_PARTITION_OTA != S61_STOCK_DOWNLOAD_SLOT) {
		ADDLOG_ERROR(LOG_FEATURE_CFG, "S61Revert: OTA slot is 0x%x (table) / 0x%x (downloader), not the stock "
					 "download slot 0x%x - refusing",
					 pt ? (unsigned)pt->partition_start_addr : 0u, (unsigned)START_ADR_OF_BK_PARTITION_OTA,
					 (unsigned)S61_STOCK_DOWNLOAD_SLOT);
		return CMD_RES_ERROR;
	}
	ADDLOG_INFO(LOG_FEATURE_CFG, "S61Revert: staging stock firmware from %s into 0x%x (%u bytes free); "
				"the stock bootloader applies it on reboot", args,
				(unsigned)pt->partition_start_addr, (unsigned)pt->partition_length);
	OTA_RequestDownloadFromHTTP(args);
	return CMD_RES_OK;
}

// One entry point, called once from Main_Init (user_main.c) right after
// CFG_InitAndLoad() so imported creds are in place before the Wi-Fi connect.
// Registers the manual command too; both are safe no-ops once Wi-Fi is set.
void Sonoff_S61_Import_Init(void) {
	//cmddetail:{"name":"S61ImportWiFi","args":"",
	//cmddetail:"descr":"Import Wi-Fi SSID/pass from the stock Sonoff S61 EF40 store.",
	//cmddetail:"fn":"Cmd_S61Import","file":"driver/drv_sonoff_s61_import.c","requires":""}
	CMD_RegisterCommand("S61ImportWiFi", Cmd_S61Import, NULL);
	//cmddetail:{"name":"S61RevertToStock","args":"[URL of stock app .rbl]",
	//cmddetail:"descr":"Stage a stock Sonoff S61 app RBL in the stock bootloader's download slot and reboot into it.",
	//cmddetail:"fn":"Cmd_S61RevertToStock","file":"driver/drv_sonoff_s61_import.c","requires":""}
	CMD_RegisterCommand("S61RevertToStock", Cmd_S61RevertToStock, NULL);
	Sonoff_S61_FirstBoot();
}
