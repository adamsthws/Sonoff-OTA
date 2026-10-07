#include "tasmota_settings.h"

#include <string.h>

uint32_t tasmota_crc32(const uint8_t *data, size_t size)
{
    /* Tasmota's GetCfgCrc32: reflected 0xEDB88320, initial 0, inverted. */
    uint32_t crc = 0;
    while (size--) {
        crc ^= *data++;
        for (int bit = 0; bit < 8; bit++) {
            crc = (crc >> 1) ^ ((crc & 1) ? 0xEDB88320u : 0);
        }
    }
    return ~crc;
}

/* Offset within the pool where entry `index` starts, or -1. */
static long entry_start(const uint8_t *pool, size_t pool_size, unsigned index)
{
    size_t position = 0;
    for (unsigned entry = 0; entry < index; entry++) {
        const uint8_t *nul = memchr(pool + position, 0, pool_size - position);
        if (nul == NULL) {
            return -1;
        }
        position = (size_t)(nul - pool) + 1;
        if (position >= pool_size) {
            return -1;
        }
    }
    return (long)position;
}

int tasmota_set_text(uint8_t *record, const tasmota_layout_t *layout, unsigned index,
                     const char *value, size_t value_len)
{
    uint8_t *pool = record + layout->pool;
    size_t size = layout->pool_size;
    if (memchr(value, 0, value_len) != NULL) {
        return TASMOTA_BAD_POOL;
    }
    long start = entry_start(pool, size, index);
    if (start < 0) {
        return TASMOTA_BAD_POOL;
    }
    const uint8_t *nul = memchr(pool + start, 0, size - (size_t)start);
    if (nul == NULL) {
        return TASMOTA_BAD_POOL;
    }
    size_t end = (size_t)(nul - pool);
    size_t old_len = end - (size_t)start;
    size_t tail = size - end;              /* from the old entry's NUL to the pool end */

    if (value_len > old_len) {
        /* Growing: the bytes pushed off the end must all be unused (zero). */
        size_t grow = value_len - old_len;
        if (grow > tail) {
            return TASMOTA_NO_ROOM;
        }
        for (size_t i = size - grow; i < size; i++) {
            if (pool[i] != 0) {
                return TASMOTA_NO_ROOM;
            }
        }
        memmove(pool + start + value_len, pool + end, tail - grow);
    } else if (value_len < old_len) {
        size_t shrink = old_len - value_len;
        memmove(pool + start + value_len, pool + end, tail);
        memset(pool + size - shrink, 0, shrink);
    }
    memcpy(pool + start, value, value_len);
    return TASMOTA_OK;
}

int tasmota_fill(uint8_t *record, size_t size, const tasmota_layout_t *layout,
                 const char *ssid, size_t ssid_len, const char *password, size_t password_len)
{
    if (layout->crc + 4 > size || layout->pool + layout->pool_size > layout->crc ||
        layout->pool_size == 0 || layout->ssid == layout->password) {
        return TASMOTA_BAD_LAYOUT;
    }
    uint32_t stored;
    memcpy(&stored, record + layout->crc, sizeof(stored));
    if (stored != tasmota_crc32(record, layout->crc)) {
        return TASMOTA_BAD_CHECKSUM;
    }
    int result = tasmota_set_text(record, layout, layout->ssid, ssid, ssid_len);
    if (result == TASMOTA_OK) {
        result = tasmota_set_text(record, layout, layout->password, password, password_len);
    }
    if (result != TASMOTA_OK) {
        return result;
    }
    uint32_t crc = tasmota_crc32(record, layout->crc);
    memcpy(record + layout->crc, &crc, sizeof(crc));
    return TASMOTA_OK;
}
