/*
 * Filling Tasmota's settings record with one Wi-Fi network.
 *
 * Pure functions with no ESP-IDF dependency, so the host tests can compile
 * them and check them against tools/autoflash/tasmota_settings.py.  The layout
 * values come from the host with each request; the bridge checks they fit.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

typedef struct {
    size_t pool;        /* offset of text_pool */
    size_t pool_size;   /* settings_text_size */
    unsigned ssid;      /* SET_STASSID1 */
    unsigned password;  /* SET_STAPWD1 */
    size_t crc;         /* offset of cfg_crc32; the checksum covers [0, crc) */
} tasmota_layout_t;

enum {
    TASMOTA_OK = 0,
    TASMOTA_BAD_LAYOUT = 1,     /* the layout does not fit the record */
    TASMOTA_BAD_CHECKSUM = 2,   /* the template arrived damaged */
    TASMOTA_BAD_POOL = 3,       /* the text pool is not in Tasmota's format */
    TASMOTA_NO_ROOM = 4,        /* the Wi-Fi details do not fit */
};

uint32_t tasmota_crc32(const uint8_t *data, size_t size);

/* Replace text entry `index`, as Tasmota's SettingsUpdateText does. */
int tasmota_set_text(uint8_t *record, const tasmota_layout_t *layout, unsigned index,
                     const char *value, size_t value_len);

/* Check the template's checksum, put the Wi-Fi in, and seal it again. */
int tasmota_fill(uint8_t *record, size_t size, const tasmota_layout_t *layout,
                 const char *ssid, size_t ssid_len, const char *password, size_t password_len);
