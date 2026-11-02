// The conversion tool's OpenBeken driver, for an eWeLink plug on a Beken chip
// that keeps its stock rt_ota bootloader.
//
// When such a plug is converted to OpenBeken over the air, its Wi-Fi SSID and
// password are still sitting in the stock EasyFlash V4 (EF40) key-value store,
// which the conversion does not touch (it lives well above the app partition
// the OTA replaces). This module reads them on first boot and hands them to
// OpenBeken's own config, so the converted plug rejoins the user's network
// with no typing and no second device.
//
// It deliberately goes through CFG_SetWiFiSSID() / CFG_SetWiFiPass() rather than
// writing the config blob itself: those setters own mainConfig_t's layout and
// checksum, so this keeps working across OpenBeken releases that change the
// config structure. The only external shapes it depends on are the stock EF40
// node format (frozen in the stock firmware) and the BK7231/BK7238 flash-read
// primitive.
//
// Reversible by design: it reads the stock store, never writes it.
//
// The EF40 reader (the KV40 node format, its CRC, the windowed scan, the
// wifi-ssid/wifi-pass keys) is eWeLink's on any Beken plug, from the same
// firmware tree; the rest is the tool's. What is one plug's own - where its
// store and download slot sit, its wiring, its meter's calibration, its kind -
// is in its table (drv_autoflash.h), in devices/<plug>/, built in beside this.
//
// Started once from platform init: Autoflash_Init().

#include "../new_common.h"
#include "../new_cfg.h"
#include "../new_pins.h"
#include "../logging/logging.h"
#include "../cmnds/cmd_public.h"
#include "../hal/hal_ota.h"
#include "../hal/hal_wifi.h"
#include "BkDriverFlash.h"
#include <easyflash.h>     // EF_START_ADDR, ENV_AREA_SIZE: where OpenBeken keeps its flash variables
#include "drv_autoflash.h"

// Raw flash read, BK7231/BK7238 HAL (hal/bk7231/hal_flashConfig_bk7231.c).
extern int beken_hal_flash_read(const unsigned int addr, void *dst, const unsigned int size);
extern int hal_flash_lock(void);
extern int hal_flash_unlock(void);

// The plug this build is for (drv_autoflash.h).
static const autoflash_device_t *const plug = &autoflash_device;

// EF40 "app" key-value store: scanned between the table's store_start and
// store_end for the keys we want. Values are stored verbatim (plaintext) on a
// provisioned plug; tools/beken/ef40.py reads the same format.
#define EF40_SCAN_CHUNK     0x1000      // 4 KiB read window
#define EF40_SCAN_OVERLAP   0x80        // keep a node from straddling two windows
#define EF40_MAX_VALUE      128

// One node: "KV40" | len u32 | crc32 u32 | name_len u8 + 0xff*3 | value_len u32
//           | name | value | trailer. crc32 (zlib/reflected, poly 0xEDB88320)
// covers the bytes from the name_len field through the end of the value.
#define KV40_HDR            20
#define KV40_OFS_NAMELEN    12
#define KV40_OFS_VALUELEN   16

// zlib's CRC-32, a nibble at a time: `crc` carried from one piece to the
// next, 0xffffffff to begin with, inverted at the end.
static uint32_t crc32_update(uint32_t crc, const uint8_t *p, int len) {
	static const uint32_t nibble[16] = {
		0x00000000, 0x1DB71064, 0x3B6E20C8, 0x26D930AC, 0x76DC4190, 0x6B6B51F4, 0x4DB26158, 0x5005713C,
		0xEDB88320, 0xF00F9344, 0xD6D6A3E8, 0xCB61B38C, 0x9B64C2B0, 0x86D3D2D4, 0xA00AE278, 0xBDBDF21C,
	};
	for (int i = 0; i < len; i++) {
		crc ^= p[i];
		crc = (crc >> 4) ^ nibble[crc & 15];
		crc = (crc >> 4) ^ nibble[crc & 15];
	}
	return crc;
}

static uint32_t ef40_crc32(const uint8_t *p, int len) {
	return ~crc32_update(0xffffffff, p, len);
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
	for (uint32_t addr = plug->store_start; addr < plug->store_end;
		 addr += EF40_SCAN_CHUNK - EF40_SCAN_OVERLAP) {
		uint32_t want = EF40_SCAN_CHUNK;
		if (addr + want > plug->store_end) want = plug->store_end - addr;
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
int Autoflash_ImportStockWiFi(void) {
	char ssid[EF40_MAX_VALUE];
	char pass[EF40_MAX_VALUE];

	if (CFG_GetWiFiSSID()[0] != 0) {
		ADDLOG_INFO(LOG_FEATURE_CFG, "AutoflashImport: Wi-Fi already set, skipping");
		return 0;
	}
	if (ef40_read_key("wifi-ssid", ssid, sizeof(ssid)) <= 0) {
		ADDLOG_INFO(LOG_FEATURE_CFG, "AutoflashImport: no stock wifi-ssid in EF40 (unprovisioned?)");
		return 0;
	}
	if (ef40_read_key("wifi-pass", pass, sizeof(pass)) < 0)
		pass[0] = 0;                          // open network: SSID without a pass is valid

	CFG_SetWiFiSSID(ssid);
	CFG_SetWiFiPass(pass);
	CFG_Save_SetupTimer();                    // persist shortly; Wi-Fi joins on next connect
	ADDLOG_INFO(LOG_FEATURE_CFG, "AutoflashImport: imported stock Wi-Fi SSID '%s' (%d-char pass)",
				ssid, (int)strlen(pass));
	return 1;
}

// Read a binary EF40 value (e.g. elecCalibCoef) into out, returns its length or -1.
int Autoflash_ReadStockBlob(const char *key, uint8_t *out, int outMax) {
	static uint8_t win[EF40_SCAN_CHUNK];
	char tmp[EF40_MAX_VALUE];
	int best = -1;
	int keyLen = (int)strlen(key);
	for (uint32_t addr = plug->store_start; addr < plug->store_end;
		 addr += EF40_SCAN_CHUNK - EF40_SCAN_OVERLAP) {
		uint32_t want = EF40_SCAN_CHUNK;
		if (addr + want > plug->store_end) want = plug->store_end - addr;
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

// The plug's name, short and full: as every firmware the conversion installs
// for good names it, the firmware and the last three bytes of its MAC, in lower
// case as ESPHome has to be (openbeken-abcdef) - or, as the interim firmware,
// as the tool's other interim firmwares are named, the last two in upper case
// (autoflash-recovery-CDEF). Both names are its hostname too (mDNS the short
// one, DHCP the long), so they are its own.
static void autoflash_name(char *out, int size, int interim) {
	unsigned char mac[6];
	WiFI_GetMacAddress((char *)mac);
	if (interim)
		snprintf(out, size, "autoflash-recovery-%02X%02X", mac[4], mac[5]);
	else
		snprintf(out, size, "openbeken-%02x%02x%02x", mac[3], mac[4], mac[5]);
}

static void autoflash_set_names(int interim) {
	char name[32];
	autoflash_name(name, sizeof(name), interim);
	CFG_SetShortDeviceName(name);
	CFG_SetDeviceName(name);
}

// The plug's wiring, from its table. Applied on first boot so the converted
// plug switches, meters and lights up straight away; the relay remembers its
// last state across power loss, as the tool sets on every plug.
static void Autoflash_ApplyPinTemplate(void) {
	for (int i = 0; i < plug->pin_count; i++) {
		const autoflash_pin_t *pin = &plug->pins[i];
		PIN_SetPinRoleForPinIndex(pin->pin, pin->role);
		if (pin->channel)
			PIN_SetPinChannelForPinIndex(pin->pin, pin->channel);
	}
	CFG_SetChannelStartupValue(plug->relay_channel, -1);   // -1: restore the last relay state
	autoflash_set_names(0);
	ADDLOG_INFO(LOG_FEATURE_CFG, "AutoflashImport: applied the %s pin template (%s); relay restores its last state",
				plug->name, plug->pins_described);
}

// First boot after conversion: creds, then - only when they were just imported,
// so a user's later changes are never overwritten - the meter calibration and
// the plug's wiring.
static int Autoflash_FirstBoot(void) {
	int imported = Autoflash_ImportStockWiFi();
	if (imported > 0) {
		plug->apply_calibration();
		Autoflash_ApplyPinTemplate();
	}
	return imported;
}

static commandResult_t Cmd_AutoflashImport(const void *context, const char *cmd,
										   const char *args, int cmdFlags) {
	return Autoflash_FirstBoot() >= 0 ? CMD_RES_OK : CMD_RES_ERROR;
}

// Revert to stock: stage a stock-app rt_ota RBL where the STOCK bootloader looks
// for an update, then let it apply on reboot. On a converted plug the stock
// bootloader is still in place and reads the table's download slot. The SDK
// picks its partition table from the flash size at runtime; on a 4 MB chip
// that is the 4M table, whose OTA partition is the stock download slot - the
// same Beken SDK map the stock firmware was built on - so OpenBeken's ordinary
// OTA download lands in the right slot. We check that before writing anything:
// on a 2 MB table the slot would be 0x132000, inside the app, and the stock
// bootloader would never see it. The stock EasyFlash store is untouched
// throughout, so the plug comes back with its own Wi-Fi, calibration and
// identity.
//     AutoflashRevertToStock http://<tool>/stock_app.rbl
static commandResult_t Cmd_AutoflashRevertToStock(const void *context, const char *cmd,
												  const char *args, int cmdFlags) {
	bk_logic_partition_t *pt = bk_flash_get_info(BK_PARTITION_OTA);
	if (args == NULL || args[0] == 0) {
		ADDLOG_ERROR(LOG_FEATURE_CFG, "AutoflashRevert: give the URL of the stock app .rbl");
		return CMD_RES_NOT_ENOUGH_ARGUMENTS;
	}
	// Two addresses must agree: the SDK partition table (what the flash size
	// selected) and START_ADR_OF_BK_PARTITION_OTA, the constant OpenBeken's own
	// OTA downloader actually writes to (hal_ota.h; the stock build says
	// 0x132000 for BK7238). A build with either wrong would put the stock image
	// where the stock bootloader never looks, so refuse rather than guess.
	if (pt == NULL || pt->partition_start_addr != plug->download_slot
		|| START_ADR_OF_BK_PARTITION_OTA != plug->download_slot) {
		ADDLOG_ERROR(LOG_FEATURE_CFG, "AutoflashRevert: OTA slot is 0x%x (table) / 0x%x (downloader), not the stock "
					 "download slot 0x%x - refusing",
					 pt ? (unsigned)pt->partition_start_addr : 0u, (unsigned)START_ADR_OF_BK_PARTITION_OTA,
					 (unsigned)plug->download_slot);
		return CMD_RES_ERROR;
	}
	ADDLOG_INFO(LOG_FEATURE_CFG, "AutoflashRevert: staging stock firmware from %s into 0x%x (%u bytes free); "
				"the stock bootloader applies it on reboot", args,
				(unsigned)pt->partition_start_addr, (unsigned)pt->partition_length);
	OTA_RequestDownloadFromHTTP(args);
	return CMD_RES_OK;
}

// What kind of plug this OpenBeken is on, for the conversion tool, which
// cannot tell by its name (every firmware it installs is named the same
// way, and a user may rename it): OK for the table's kind, an error for
// any other. An OpenBeken the tool did not install does not know the
// command at all. Changes nothing.
//     AutoflashKind <kind>
static commandResult_t Cmd_AutoflashKind(const void *context, const char *cmd,
										 const char *args, int cmdFlags) {
	if (args == NULL || args[0] == 0) {
		return CMD_RES_NOT_ENOUGH_ARGUMENTS;
	}
	return strcmp(args, plug->kind) == 0 ? CMD_RES_OK : CMD_RES_BAD_ARGUMENT;
}

// The interim firmware. A run of the conversion tool to ESPHome installs this
// OpenBeken first, to back the plug up through its flash reads, then has it
// fetch ESPHome; a run to OpenBeken installs the same image to keep. The tool
// tells the two apart by a marker sector it sends after the image, in the same
// update (tools/beken/interim_marker.py has the format): the stock updater
// writes the whole file into the download slot, and the stock bootloader,
// having applied the image, erases only the slot's first sector, so the marker
// is still there on the first boot. The marker names the app its image
// installs (its size and FNV-1a hash, from the image's RBL header), and only a
// marker for the app running counts: one left by anything else, or for
// another image, never makes a plug the interim firmware, and is erased.
// Found, it names the plug as the interim firmware, autoflash-recovery-CDEF -
// and that name is the record: no
// field of OpenBeken's config is the module's to keep a flag in (its flags and
// unused bytes are upstream's to give a meaning), and a name set through the
// setters, as the Wi-Fi is, survives any change of config layout. A plug
// renamed is the user's own, and no longer the interim firmware.
#define AUTOFLASH_SECTOR          0x1000
#define AUTOFLASH_MARKER_MAGIC    "AFINTERM"
#define AUTOFLASH_MARKER_VERSION  2
#define AUTOFLASH_MARKER_SIZE     28
#define AUTOFLASH_STRIPE          34         // the app is stored CRC-striped: 32 bytes, then their 2-byte CRC
#define RBL_HEADER                96

static uint32_t autoflash_u32(const uint8_t *p) {
	return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24);
}

int Autoflash_IsInterim(void) {
	char name[32];
	autoflash_name(name, sizeof(name), 1);
	return strcmp(CFG_GetShortDeviceName(), name) == 0 && strcmp(CFG_GetDeviceName(), name) == 0;
}

// The download slot, where the stock bootloader looks: or 0 on a build whose
// slot is elsewhere, where there is no marker to look for.
static bk_logic_partition_t *autoflash_slot(void) {
	bk_logic_partition_t *pt = bk_flash_get_info(BK_PARTITION_OTA);
	return pt && pt->partition_start_addr == plug->download_slot ? pt : 0;
}

// Whether the sector at `offset` in the slot is a marker: its magic, version
// and CRC, and at the 4 KB boundary after the image size it gives. The app it
// is for, its size and hash, in `appSize` and `appHash` where given.
static int autoflash_marker_at(const bk_logic_partition_t *slot, uint32_t offset, uint32_t *appSize,
							   uint32_t *appHash) {
	uint8_t head[AUTOFLASH_MARKER_SIZE];
	if (beken_hal_flash_read(slot->partition_start_addr + offset, head, sizeof(head)) != 0)
		return 0;
	if (memcmp(head, AUTOFLASH_MARKER_MAGIC, 8) != 0 || autoflash_u32(head + 8) != AUTOFLASH_MARKER_VERSION)
		return 0;
	if (autoflash_u32(head + 24) != ef40_crc32(head, 24))
		return 0;
	uint32_t imageSize = autoflash_u32(head + 12);
	if (appSize)
		*appSize = autoflash_u32(head + 16);
	if (appHash)
		*appHash = autoflash_u32(head + 20);
	return imageSize > 0 && (imageSize + AUTOFLASH_SECTOR - 1) / AUTOFLASH_SECTOR * AUTOFLASH_SECTOR == offset;
}

// Whether a marker's app is the one installed - the firmware running: the
// FNV-1a hash of the app partition's first `appSize` bytes, which the
// bootloader checked against the same hash once it wrote them. The partition
// is read raw, CRC stripes and all, so each 34 bytes give 32. 1 if it is, 0
// if not, -1 if that cannot be told here.
static int autoflash_marker_for_this_app(uint32_t appSize, uint32_t appHash) {
	static uint8_t buf[AUTOFLASH_STRIPE * 30];
	bk_logic_partition_t *app = bk_flash_get_info(BK_PARTITION_APPLICATION);
	if (app == 0 || app->partition_start_addr % AUTOFLASH_STRIPE != 0)
		return -1;
	if (appSize == 0 || appSize > app->partition_length / AUTOFLASH_STRIPE * 32)
		return 0;
	uint32_t hash = 0x811c9dc5, left = appSize, at = app->partition_start_addr;
	while (left > 0) {
		uint32_t stripes = (left + 31) / 32, want = sizeof(buf);
		if (stripes * AUTOFLASH_STRIPE < want)
			want = stripes * AUTOFLASH_STRIPE;
		if (beken_hal_flash_read(at, buf, want) != 0)
			return -1;
		for (uint32_t i = 0; i < want && left > 0; i += AUTOFLASH_STRIPE) {
			uint32_t n = left < 32 ? left : 32;
			for (uint32_t j = 0; j < n; j++)
				hash = (hash ^ buf[i + j]) * 0x01000193;
			left -= n;
		}
		at += want;
	}
	return hash == appHash;
}

static void autoflash_erase(uint32_t address, uint32_t size) {
	hal_flash_lock();
	bk_flash_abs_addr_erase(address, size);
	hal_flash_unlock();
}

// Every marker in the slot from `from` on erased; how many there were. The
// slot's first sector is the image's header, never a marker.
static int autoflash_erase_markers(const bk_logic_partition_t *slot, uint32_t from) {
	int erased = 0;
	if (from < AUTOFLASH_SECTOR)
		from = AUTOFLASH_SECTOR;
	for (uint32_t offset = from; offset + AUTOFLASH_SECTOR <= slot->partition_length; offset += AUTOFLASH_SECTOR) {
		if (autoflash_marker_at(slot, offset, 0, 0)) {
			autoflash_erase(slot->partition_start_addr + offset, AUTOFLASH_SECTOR);
			erased++;
		}
	}
	return erased;
}

// The first boot after an install the tool sent with a marker: named as the
// interim firmware, the names saved, and only then the marker erased, so a
// power cut in between leaves it to be found again. Before Wi-Fi starts, so
// the plug asks for its address under that name from the first. A marker for
// another app is erased: the bootloader installs an image before any app
// starts, so one whose app is not running now never will be.
static void Autoflash_TakeInterimMarker(void) {
	bk_logic_partition_t *slot = autoflash_slot();
	uint32_t appSize, appHash;
	if (slot == 0)
		return;
	for (uint32_t offset = AUTOFLASH_SECTOR; offset + AUTOFLASH_SECTOR <= slot->partition_length;
		 offset += AUTOFLASH_SECTOR) {
		if (!autoflash_marker_at(slot, offset, &appSize, &appHash))
			continue;
		uint32_t began = xTaskGetTickCount();
		int ours = autoflash_marker_for_this_app(appSize, appHash);
		unsigned ms = (unsigned)((xTaskGetTickCount() - began) * portTICK_PERIOD_MS);
		if (ours < 0) {
			ADDLOG_WARN(LOG_FEATURE_CFG, "AutoflashImport: a marker at 0x%x, but the app cannot be read to tell "
						"whose; left as it is", (unsigned)(slot->partition_start_addr + offset));
			continue;
		}
		if (!ours) {
			autoflash_erase(slot->partition_start_addr + offset, AUTOFLASH_SECTOR);
			ADDLOG_WARN(LOG_FEATURE_CFG, "AutoflashImport: erased a marker for another app (checked in %u ms; at "
						"0x%x, %u bytes, hash 0x%08x)", ms, (unsigned)(slot->partition_start_addr + offset),
						(unsigned)appSize, (unsigned)appHash);
			continue;
		}
		autoflash_set_names(1);
		CFG_Save_IfThereArePendingChanges();
		autoflash_erase_markers(slot, offset);
		ADDLOG_INFO(LOG_FEATURE_CFG, "AutoflashImport: the interim firmware (its app checked in %u ms), named %s, "
					"marker at 0x%x", ms, CFG_GetShortDeviceName(), (unsigned)(slot->partition_start_addr + offset));
		return;
	}
}

// Whether the slot holds a whole rt_ota app image, as the bootloader will
// apply it: the header's CRC-32, and the CRC-32 of the body it gives. Where it
// ends, in `end` (from the slot's start).
static int autoflash_slot_holds_image(const bk_logic_partition_t *slot, uint32_t *end) {
	static uint8_t buf[1024];
	uint8_t header[RBL_HEADER];
	if (beken_hal_flash_read(slot->partition_start_addr, header, sizeof(header)) != 0)
		return 0;
	if (memcmp(header, "RBL\0", 4) != 0 || strncmp((const char *)header + 12, "app", 16) != 0
		|| autoflash_u32(header + 92) != ef40_crc32(header, 92))
		return 0;
	uint32_t body = autoflash_u32(header + 88);
	if (body == 0 || RBL_HEADER + body > slot->partition_length)
		return 0;
	uint32_t crc = 0xffffffff;
	for (uint32_t done = 0; done < body; ) {
		uint32_t want = body - done < sizeof(buf) ? body - done : sizeof(buf);
		if (beken_hal_flash_read(slot->partition_start_addr + RBL_HEADER + done, buf, want) != 0)
			return 0;
		crc = crc32_update(crc, buf, want);
		done += want;
	}
	*end = RBL_HEADER + body;
	return ~crc == autoflash_u32(header + 76);
}

// Called by OpenBeken's HTTP OTA once a download is written, just before the
// restart that applies it (tools/beken/openbeken/'s hook patch). The interim
// firmware, its work done, leaves nothing of itself: its settings, its flash
// variables and any marker are erased, so the firmware it fetched - ESPHome,
// or the stock one - starts with nothing of it, and a later OpenBeken starts
// afresh, named by its first boot's import. Only once the whole image is in
// the slot: a download cut off leaves the plug the interim firmware still, to
// be sent it again, and a power cut after this still applies the image. Any
// other OpenBeken - a user's own, updating itself - is left as it is.
void HAL_OTA_BeforeReboot(int ok) {
	bk_logic_partition_t *slot = autoflash_slot();
	bk_logic_partition_t *config = bk_flash_get_info(BK_PARTITION_NET_PARAM);
	uint32_t end = 0;
	if (!Autoflash_IsInterim() || slot == 0)
		return;
	if (!ok || !autoflash_slot_holds_image(slot, &end)) {
		ADDLOG_WARN(LOG_FEATURE_CFG, "AutoflashImport: the download is not a whole image; the interim firmware "
					"stays");
		return;
	}
	if (config == 0 || config->partition_start_addr != plug->settings_sector
		|| config->partition_length != AUTOFLASH_SECTOR) {
		ADDLOG_ERROR(LOG_FEATURE_CFG, "AutoflashImport: OpenBeken's settings are not at 0x%x; left as they are",
					 (unsigned)plug->settings_sector);
		return;
	}
	int markers = autoflash_erase_markers(slot, (end + AUTOFLASH_SECTOR - 1) / AUTOFLASH_SECTOR * AUTOFLASH_SECTOR);
	autoflash_erase(EF_START_ADDR, ENV_AREA_SIZE);
	autoflash_erase(config->partition_start_addr, config->partition_length);
	ADDLOG_INFO(LOG_FEATURE_CFG, "AutoflashImport: the interim firmware erased its settings (0x%x), its flash "
				"variables (0x%x+0x%x) and %d marker(s)", (unsigned)config->partition_start_addr,
				(unsigned)EF_START_ADDR, (unsigned)ENV_AREA_SIZE, markers);
}

// Whether this OpenBeken is the interim firmware, for the conversion tool:
// OK when it is, an error when not. `off` makes it this plug's OpenBeken for
// good - named openbeken-abcdef - for a run that finds it and is to end on
// OpenBeken; `on` makes it the interim firmware, for the run that sent it as
// one when its marker did not arrive (its update cut off in its last few
// kilobytes, after the image). Its DHCP and mDNS names follow on its next start.
//     AutoflashInterim
//     AutoflashInterim off
//     AutoflashInterim on
static commandResult_t Cmd_AutoflashInterim(const void *context, const char *cmd,
											const char *args, int cmdFlags) {
	if (args == NULL || args[0] == 0) {
		return Autoflash_IsInterim() ? CMD_RES_OK : CMD_RES_ERROR;
	}
	int interim = strcmp(args, "on") == 0;
	if (!interim && strcmp(args, "off") != 0) {
		return CMD_RES_BAD_ARGUMENT;
	}
	if (Autoflash_IsInterim() != interim) {
		autoflash_set_names(interim);
		CFG_Save_IfThereArePendingChanges();
		ADDLOG_INFO(LOG_FEATURE_CFG, "AutoflashImport: %s the interim firmware; named %s",
					interim ? "now" : "no longer", CFG_GetShortDeviceName());
	}
	return CMD_RES_OK;
}

// One entry point, called once from Main_Init (user_main.c) right after
// CFG_InitAndLoad() so imported creds are in place before the Wi-Fi connect.
// Registers the commands too; the import is a safe no-op once Wi-Fi is set.
void Autoflash_Init(void) {
	//cmddetail:{"name":"AutoflashImportWiFi","args":"",
	//cmddetail:"descr":"Import Wi-Fi SSID/pass, and on a first import the meter calibration and wiring, from the stock EF40 store.",
	//cmddetail:"fn":"Cmd_AutoflashImport","file":"driver/drv_autoflash.c","requires":""}
	CMD_RegisterCommand("AutoflashImportWiFi", Cmd_AutoflashImport, NULL);
	//cmddetail:{"name":"AutoflashRevertToStock","args":"[URL of stock app .rbl]",
	//cmddetail:"descr":"Stage a stock app RBL in the stock bootloader's download slot and reboot into it.",
	//cmddetail:"fn":"Cmd_AutoflashRevertToStock","file":"driver/drv_autoflash.c","requires":""}
	CMD_RegisterCommand("AutoflashRevertToStock", Cmd_AutoflashRevertToStock, NULL);
	//cmddetail:{"name":"AutoflashKind","args":"[kind]",
	//cmddetail:"descr":"OK when this OpenBeken is on the given kind of plug, for the conversion tool.",
	//cmddetail:"fn":"Cmd_AutoflashKind","file":"driver/drv_autoflash.c","requires":""}
	CMD_RegisterCommand("AutoflashKind", Cmd_AutoflashKind, NULL);
	//cmddetail:{"name":"AutoflashInterim","args":"[on|off]",
	//cmddetail:"descr":"OK when this OpenBeken is the conversion tool's interim firmware; off makes it the plug's own, on the interim firmware.",
	//cmddetail:"fn":"Cmd_AutoflashInterim","file":"driver/drv_autoflash.c","requires":""}
	CMD_RegisterCommand("AutoflashInterim", Cmd_AutoflashInterim, NULL);
	Autoflash_FirstBoot();
	Autoflash_TakeInterimMarker();
}
