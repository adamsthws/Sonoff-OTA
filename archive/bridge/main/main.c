/*
 * S60 one-shot OTA bridge.
 *
 * The bridge is an ESP32-C3 application image intended for an existing S60
 * OTA slot. It does not contain or replace a bootloader or partition table.
 *
 * Recovery invariant:
 *   1. At startup, confirm this bridge so bootloader rollback cannot discard it.
 *   2. Select the untouched peer app slot for the next boot.
 *   3. Before erasing that peer for an upload, select this bridge again.
 *   4. Only select the uploaded app after esp_ota_end validates it.
 *
 * This cannot protect against failure before app_main starts, but it makes a
 * later Wi-Fi/HTTP failure recoverable by power-cycling back to stock.
 *
 * v4: the stock firmware keeps its Wi-Fi details where ESP-IDF does
 * (nvs.net80211 sta.ssid / sta.pswd).  The bridge reads them, read-only, and
 * joins that network as well as making its own, so the host can upload over
 * the home network.  If they are missing or the join fails within
 * STA_WINDOW_MS, the bridge drops to its own network alone, exactly as v3.
 * Wi-Fi's own NVS use stays off (sdkconfig.defaults): nothing Wi-Fi does is
 * saved, and the stock firmware's data is only read.
 *
 * v5: POST /tasmota-settings takes a Tasmota settings template (no Wi-Fi in
 * it), fills in the network the bridge joined, and saves it where Tasmota
 * reads its settings at boot (NVS "main"/"Settings").  Tasmota then joins the
 * home network on its first boot instead of making a setup network.  Only
 * done once the bridge has joined with those details, so they are known good.
 *
 * 4.4.1 (tried as "v6"): from here the bridge is named by the version the
 * updater is given, and the next is 4.4.2.  With the stock Wi-Fi details it
 * joins that network alone and raises its own only if it has had no address
 * there for STA_WINDOW_MS, at start-up or later.  v5 ran both at once, and on a plug the joined network
 * dropped out for seconds at a time while the bridge's own was up.  Without
 * stock details it raises its own network at once, as before.
 *
 * 4.4.2: GET /flash streams the whole flash chip from address 0, GET
 * /flash-sha256 the SHA-256 of it worked out here, and GET /efuse the eFuse
 * read registers, all read-only, so the run keeps a backup of the stock plug -
 * checked byte for byte against the chip - before Tasmota is written over its
 * Sonoff firmware.
 *
 * BRIDGE_FLASH_READOUT (research build, never shipped): the same reads without
 * /update or /tasmota-settings, so a stock plug can be imaged without being
 * converted.  The only flash it writes is the otadata selection at startup,
 * which returns the plug to stock on the next power cycle.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_err.h"
#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "tasmota_settings.h"
#include "esp_flash.h"
#include "mbedtls/sha256.h"
#include "soc/efuse_reg.h"
#include "soc/soc.h"

#define BRIDGE_AP_PASSWORD "s60-ota-bridge"
#define BRIDGE_MAX_IMAGE_SIZE 0x1f0000
#define UPLOAD_BUFFER_SIZE 4096
#define BRIDGE_HOSTNAME "s60-ota-bridge"
/* How long to try the home network before keeping to the bridge's own. */
#define STA_WINDOW_MS 45000
/* The one version: the wrapper the updater is given and the app description
 * (PROJECT_VER in ../CMakeLists.txt) carry the same; a test keeps them equal. */
#define BRIDGE_VERSION "4.4.2"
#ifdef BRIDGE_FLASH_READOUT
#define BRIDGE_NAME "bridge " BRIDGE_VERSION " flash readout"
#else
#define BRIDGE_NAME "bridge " BRIDGE_VERSION
#endif

static const char *TAG = "s60_bridge";
static const esp_partition_t *s_running;
static const esp_partition_t *s_peer;
static volatile bool s_sta_trying;
static volatile uint32_t s_sta_ip; /* network byte order; 0 until joined */
static volatile bool s_tasmota_settings_saved;
/* The bridge's own network, raised only when the home network fails. */
static wifi_config_t s_ap_config;
#define TASMOTA_SETTINGS_SIZE 4096
static uint8_t s_settings[TASMOTA_SETTINGS_SIZE];
/* Keep the flash buffer out of the HTTP server task's relatively small stack.
 * A large automatic buffer can reset the bridge on handler entry, before the
 * recovery slot is pinned. */
static char s_upload_buffer[UPLOAD_BUFFER_SIZE];

static const char INDEX_HTML[] =
    "<!doctype html><meta name=viewport content='width=device-width'>"
    "<title>S60 OTA Bridge</title>"
    "<style>body{font:16px system-ui;max-width:42rem;margin:3rem auto;padding:0 1rem}"
    "button{padding:.7rem 1rem}code{background:#eee;padding:.1rem .3rem}</style>"
    "<h1>S60 OTA Bridge</h1>"
    "<p>The untouched stock slot is selected for the next power cycle.</p>"
    "<p>Choose a native ESP32-C3 application image no larger than 0x1f0000 "
    "bytes. The peer slot is erased only after you press Upload.</p>"
    "<input id=f type=file accept='.bin,application/octet-stream'> "
    "<button id=u>Upload</button><pre id=o></pre>"
    "<script>u.onclick=async()=>{let x=f.files[0];if(!x){o.textContent='Choose a file';return}"
    "if(!confirm('Overwrite the stock slot with '+x.name+' ('+x.size+' bytes)?'))return;"
    "u.disabled=true;o.textContent='Uploading. Do not remove power...';try{let r=await fetch('/update',"
    "{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:x});"
    "o.textContent=await r.text()}catch(e){o.textContent='Connection ended: '+e+'\nReconnect to the bridge and retry.'}}</script>";

static void delayed_restart(void *unused)
{
    (void)unused;
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
}

static esp_err_t root_handler(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, INDEX_HTML, HTTPD_RESP_USE_STRLEN);
}

#ifdef BRIDGE_NVS_READOUT
/* Research build only: stream the stock NVS partition exactly as it is, so the
 * host can see where the stock firmware keeps its Wi-Fi details.  Read-only:
 * nothing here writes flash, and NVS is never initialised. */
static esp_err_t nvs_handler(httpd_req_t *req)
{
    const esp_partition_t *nvs = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_NVS, NULL);
    if (nvs == NULL) {
        httpd_resp_set_status(req, "404 Not Found");
        return httpd_resp_sendstr(req, "No NVS partition.\n");
    }
    httpd_resp_set_type(req, "application/octet-stream");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    for (size_t offset = 0; offset < nvs->size; offset += sizeof(s_upload_buffer)) {
        size_t length = nvs->size - offset < sizeof(s_upload_buffer)
                            ? nvs->size - offset
                            : sizeof(s_upload_buffer);
        if (esp_partition_read(nvs, offset, s_upload_buffer, length) != ESP_OK ||
            httpd_resp_send_chunk(req, s_upload_buffer, length) != ESP_OK) {
            httpd_resp_send_chunk(req, NULL, 0);
            return ESP_FAIL;
        }
    }
    return httpd_resp_send_chunk(req, NULL, 0);
}
#endif

/* Stream the whole flash chip, bootloader and partition table included,
 * exactly as it reads.  Read-only. */
static esp_err_t flash_handler(httpd_req_t *req)
{
    uint32_t size = 0;
    if (esp_flash_get_size(NULL, &size) != ESP_OK || size == 0) {
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "Could not read the flash size.\n");
    }
    char size_text[12];
    snprintf(size_text, sizeof(size_text), "%lu", (unsigned long)size);
    httpd_resp_set_type(req, "application/octet-stream");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_set_hdr(req, "X-Flash-Size", size_text);
    for (uint32_t offset = 0; offset < size; offset += sizeof(s_upload_buffer)) {
        uint32_t length = size - offset < sizeof(s_upload_buffer)
                              ? size - offset
                              : sizeof(s_upload_buffer);
        if (esp_flash_read(NULL, s_upload_buffer, offset, length) != ESP_OK ||
            httpd_resp_send_chunk(req, s_upload_buffer, length) != ESP_OK) {
            /* End without the final chunk so the host sees a broken transfer. */
            return ESP_FAIL;
        }
    }
    return httpd_resp_send_chunk(req, NULL, 0);
}

/* The SHA-256 of the whole flash chip, read here, in lower-case hex, so the
 * host can check what /flash sent it byte for byte.  Read-only. */
static esp_err_t flash_sha256_handler(httpd_req_t *req)
{
    uint32_t size = 0;
    if (esp_flash_get_size(NULL, &size) != ESP_OK || size == 0) {
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "Could not read the flash size.\n");
    }
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    mbedtls_sha256_starts(&sha, 0);
    for (uint32_t offset = 0; offset < size; offset += sizeof(s_upload_buffer)) {
        uint32_t length = size - offset < sizeof(s_upload_buffer)
                              ? size - offset
                              : sizeof(s_upload_buffer);
        if (esp_flash_read(NULL, s_upload_buffer, offset, length) != ESP_OK) {
            mbedtls_sha256_free(&sha);
            httpd_resp_set_status(req, "500 Internal Server Error");
            return httpd_resp_sendstr(req, "Could not read the flash.\n");
        }
        mbedtls_sha256_update(&sha, (const unsigned char *)s_upload_buffer, length);
    }
    unsigned char digest[32];
    mbedtls_sha256_finish(&sha, digest);
    mbedtls_sha256_free(&sha);
    char text[2 * sizeof(digest) + 2];
    for (size_t i = 0; i < sizeof(digest); i++) {
        snprintf(text + 2 * i, 3, "%02x", digest[i]);
    }
    text[2 * sizeof(digest)] = '\n';
    text[2 * sizeof(digest) + 1] = 0;
    char size_text[12];
    snprintf(size_text, sizeof(size_text), "%lu", (unsigned long)size);
    httpd_resp_set_type(req, "text/plain");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_set_hdr(req, "X-Flash-Size", size_text);
    return httpd_resp_sendstr(req, text);
}

/* The eFuse read registers (EFUSE_RD_WR_DIS_REG to
 * EFUSE_RD_SYS_PART2_DATA7_REG), little-endian words in register order.
 * Reading them does not program anything. */
static esp_err_t efuse_handler(httpd_req_t *req)
{
    size_t length = 0;
    for (uint32_t reg = EFUSE_RD_WR_DIS_REG; reg <= EFUSE_RD_SYS_PART2_DATA7_REG; reg += 4) {
        uint32_t word = REG_READ(reg);
        memcpy(s_upload_buffer + length, &word, sizeof(word));
        length += sizeof(word);
    }
    httpd_resp_set_type(req, "application/octet-stream");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, s_upload_buffer, length);
}

/* Plain-text state for the host: which networks the bridge is on. */
static esp_err_t status_handler(httpd_req_t *req)
{
    char body[128];
    uint32_t ip = s_sta_ip;
    if (ip != 0) {
        snprintf(body, sizeof(body), BRIDGE_NAME "\nhome network: joined " IPSTR "\n%s",
                 IP2STR((esp_ip4_addr_t *)&ip),
                 s_tasmota_settings_saved ? "tasmota settings: saved\n" : "");
    } else {
        snprintf(body, sizeof(body), BRIDGE_NAME "\nhome network: %s\n",
                 s_sta_trying ? "joining" : "not joined");
    }
    httpd_resp_set_type(req, "text/plain");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_sendstr(req, body);
}

static esp_err_t settings_reply(httpd_req_t *req, const char *status, const char *message)
{
    memset(s_settings, 0, sizeof(s_settings));
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "text/plain");
    return httpd_resp_sendstr(req, message);
}

static bool query_number(const char *query, const char *key, size_t *value)
{
    char text[12];
    if (httpd_query_key_value(query, key, text, sizeof(text)) != ESP_OK) {
        return false;
    }
    char *end = NULL;
    unsigned long number = strtoul(text, &end, 10);
    if (end == text || *end != 0) {
        return false;
    }
    *value = number;
    return true;
}

/* Fill a Tasmota settings template with the network the bridge joined and
 * save it where Tasmota reads it at boot.  The layout comes in the query, so
 * the bridge needs no knowledge of Tasmota's release. */
static esp_err_t tasmota_settings_handler(httpd_req_t *req)
{
    if (s_sta_ip == 0) {
        return settings_reply(req, "409 Conflict",
                              "The bridge has not joined a network, so there are no proven "
                              "Wi-Fi details to give Tasmota. Nothing was saved.\n");
    }
    char query[96];
    size_t pool, pool_size, ssid_index, password_index, crc;
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK ||
        !query_number(query, "pool", &pool) || !query_number(query, "pool_size", &pool_size) ||
        !query_number(query, "ssid", &ssid_index) ||
        !query_number(query, "password", &password_index) || !query_number(query, "crc", &crc)) {
        return settings_reply(req, "400 Bad Request", "Missing layout. Nothing was saved.\n");
    }
    if (req->content_len != TASMOTA_SETTINGS_SIZE) {
        return settings_reply(req, "400 Bad Request", "Wrong size. Nothing was saved.\n");
    }
    size_t received = 0;
    while (received < TASMOTA_SETTINGS_SIZE) {
        int got = httpd_req_recv(req, (char *)s_settings + received, TASMOTA_SETTINGS_SIZE - received);
        if (got == HTTPD_SOCK_ERR_TIMEOUT) {
            continue;
        }
        if (got <= 0) {
            return settings_reply(req, "400 Bad Request", "Upload interrupted. Nothing was saved.\n");
        }
        received += (size_t)got;
    }

    wifi_config_t joined = {0};
    if (esp_wifi_get_config(WIFI_IF_STA, &joined) != ESP_OK) {
        return settings_reply(req, "500 Internal Server Error", "No Wi-Fi details. Nothing was saved.\n");
    }
    const tasmota_layout_t layout = {
        .pool = pool, .pool_size = pool_size,
        .ssid = (unsigned)ssid_index, .password = (unsigned)password_index, .crc = crc,
    };
    size_t ssid_len = strnlen((const char *)joined.sta.ssid, sizeof(joined.sta.ssid));
    size_t password_len = strnlen((const char *)joined.sta.password, sizeof(joined.sta.password));
    int filled = tasmota_fill(s_settings, sizeof(s_settings), &layout,
                              (const char *)joined.sta.ssid, ssid_len,
                              (const char *)joined.sta.password, password_len);
    memset(&joined, 0, sizeof(joined));
    if (filled != TASMOTA_OK) {
        static const char *const reasons[] = {
            "", "The layout does not fit the record.", "The template arrived damaged.",
            "The template's text settings are not in Tasmota's format.",
            "The Wi-Fi details do not fit in Tasmota's text settings.",
        };
        char message[120];
        snprintf(message, sizeof(message), "%s Nothing was saved.\n",
                 filled < 5 ? reasons[filled] : "Unknown error.");
        return settings_reply(req, "400 Bad Request", message);
    }

    esp_err_t err = nvs_flash_init();
    nvs_handle_t handle;
    if (err == ESP_OK) {
        err = nvs_open("main", NVS_READWRITE, &handle);
        if (err == ESP_OK) {
            err = nvs_set_blob(handle, "Settings", s_settings, sizeof(s_settings));
            if (err == ESP_OK) {
                err = nvs_commit(handle);
            }
            nvs_close(handle);
        }
        nvs_flash_deinit();
    }
    if (err != ESP_OK) {
        char message[120];
        snprintf(message, sizeof(message), "Could not save Tasmota's settings (%s).\n",
                 esp_err_to_name(err));
        return settings_reply(req, "500 Internal Server Error", message);
    }
    s_tasmota_settings_saved = true;
    ESP_LOGI(TAG, "Tasmota settings saved");
    return settings_reply(req, "200 OK", "Tasmota settings saved with the home network.\n");
}

static esp_err_t upload_error(httpd_req_t *req, const char *message)
{
    ESP_LOGE(TAG, "%s", message);
    httpd_resp_set_status(req, "400 Bad Request");
    return httpd_resp_sendstr(req, message);
}

static esp_err_t update_handler(httpd_req_t *req)
{
    if (req->content_len <= 0 || req->content_len > BRIDGE_MAX_IMAGE_SIZE ||
        req->content_len > s_peer->size) {
        return upload_error(req, "Invalid or oversized image. Nothing was erased.\n");
    }

    /* If power is lost after peer erasure begins, reboot this bridge. */
    esp_err_t err = esp_ota_set_boot_partition(s_running);
    if (err != ESP_OK) {
        return upload_error(req, "Could not pin the bridge as recovery image. Nothing was erased.\n");
    }

    esp_ota_handle_t handle = 0;
    err = esp_ota_begin(s_peer, req->content_len, &handle);
    if (err != ESP_OK) {
        return upload_error(req, "Could not begin OTA. The bridge remains selected.\n");
    }

    int remaining = req->content_len;
    while (remaining > 0) {
        int wanted = remaining < (int)sizeof(s_upload_buffer)
                         ? remaining
                         : (int)sizeof(s_upload_buffer);
        int received = httpd_req_recv(req, s_upload_buffer, wanted);
        if (received == HTTPD_SOCK_ERR_TIMEOUT) {
            continue;
        }
        if (received <= 0) {
            esp_ota_abort(handle);
            return upload_error(req, "Upload interrupted. Reconnect to the bridge and retry.\n");
        }
        err = esp_ota_write(handle, s_upload_buffer, received);
        if (err != ESP_OK) {
            esp_ota_abort(handle);
            return upload_error(req, "Flash write failed. Reconnect to the bridge and retry.\n");
        }
        remaining -= received;
    }

    err = esp_ota_end(handle);
    if (err != ESP_OK) {
        return upload_error(req, "ESP image validation failed. The bridge remains selected.\n");
    }
    err = esp_ota_set_boot_partition(s_peer);
    if (err != ESP_OK) {
        return upload_error(req, "Image is valid, but selecting it failed. The bridge remains selected.\n");
    }

    httpd_resp_set_type(req, "text/plain");
    httpd_resp_sendstr(req, "Upload verified. Rebooting into the new application...\n");
    xTaskCreate(delayed_restart, "restart", 2048, NULL, 5, NULL);
    return ESP_OK;
}

/* The stock firmware's saved station details, if it saved any.  Read-only:
 * NVS is opened NVS_READONLY and never erased, even if initialising fails. */
static bool read_stock_wifi(wifi_config_t *sta)
{
    esp_err_t err = nvs_flash_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "stock NVS not readable (%s)", esp_err_to_name(err));
        return false;
    }
    bool found = false;
    nvs_handle_t handle;
    if (nvs_open("nvs.net80211", NVS_READONLY, &handle) == ESP_OK) {
        struct {
            uint32_t len;
            uint8_t ssid[32];
        } ssid;
        char password[65];
        size_t ssid_size = sizeof(ssid), password_size = sizeof(password);
        if (nvs_get_blob(handle, "sta.ssid", &ssid, &ssid_size) == ESP_OK &&
            ssid_size == sizeof(ssid) && ssid.len > 0 && ssid.len <= 32 &&
            nvs_get_blob(handle, "sta.pswd", password, &password_size) == ESP_OK &&
            password_size <= sizeof(password)) {
            memcpy(sta->sta.ssid, ssid.ssid, ssid.len);
            password[sizeof(password) - 1] = 0;
            strlcpy((char *)sta->sta.password, password, sizeof(sta->sta.password));
            found = true;
        }
        memset(password, 0, sizeof(password));
        nvs_close(handle);
    }
    nvs_flash_deinit();
    return found;
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START && s_sta_trying) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_sta_ip = 0;
        if (s_sta_trying) {
            esp_wifi_connect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        s_sta_ip = ((ip_event_got_ip_t *)data)->ip_info.ip.addr;
        ESP_LOGI(TAG, "joined the home network: " IPSTR,
                 IP2STR(&((ip_event_got_ip_t *)data)->ip_info.ip));
    }
}

/* Raise the bridge's own network once the home network has given the bridge
 * no address for STA_WINDOW_MS - at start-up, or after losing it - so there
 * is always a way in.  It then keeps to its own network, as v5 did. */
static void station_watch(void *unused)
{
    (void)unused;
    TickType_t without = 0;
    while (without < pdMS_TO_TICKS(STA_WINDOW_MS)) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        without = s_sta_ip != 0 ? 0 : without + pdMS_TO_TICKS(1000);
    }
    ESP_LOGW(TAG, "no address on the home network; raising the bridge's own");
    s_sta_trying = false;
    esp_wifi_disconnect();
    esp_wifi_set_mode(WIFI_MODE_AP);
    esp_wifi_set_config(WIFI_IF_AP, &s_ap_config);
    ESP_LOGI(TAG, "AP ready: %s", s_ap_config.ap.ssid);
    vTaskDelete(NULL);
}

static esp_err_t start_access_point(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    if (esp_netif_create_default_wifi_ap() == NULL) {
        return ESP_FAIL;
    }

    wifi_config_t station = {0};
    bool have_station = read_stock_wifi(&station);
    esp_netif_t *sta_netif = have_station ? esp_netif_create_default_wifi_sta() : NULL;
    if (sta_netif != NULL) {
        esp_netif_set_hostname(sta_netif, BRIDGE_HOSTNAME);
    }

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi_event, NULL));

    uint8_t mac[6];
    ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP));
    wifi_config_t *config = &s_ap_config;
    snprintf((char *)config->ap.ssid, sizeof(config->ap.ssid),
             "S60-OTA-Bridge-%02X%02X", mac[4], mac[5]);
    config->ap.ssid_len = strlen((char *)config->ap.ssid);
    strlcpy((char *)config->ap.password, BRIDGE_AP_PASSWORD,
            sizeof(config->ap.password));
    config->ap.channel = 1;
    config->ap.max_connection = 2;
    config->ap.authmode = WIFI_AUTH_WPA2_PSK;
    config->ap.pmf_cfg.capable = true;
    config->ap.pmf_cfg.required = false;

    if (sta_netif != NULL) {
        station.sta.pmf_cfg.capable = true;
        station.sta.pmf_cfg.required = false;
        s_sta_trying = true;
        ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &station));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, config));
    }
    memset(&station, 0, sizeof(station));
    ESP_ERROR_CHECK(esp_wifi_start());
    if (sta_netif != NULL) {
        ESP_LOGI(TAG, "joining the home network; own network only if that fails");
        xTaskCreate(station_watch, "sta_watch", 2048, NULL, 5, NULL);
    } else {
        ESP_LOGI(TAG, "AP ready: %s", config->ap.ssid);
    }
    return ESP_OK;
}

static esp_err_t start_web_server(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.stack_size = 8192;
    config.recv_wait_timeout = 20;
    config.send_wait_timeout = 20;

    httpd_handle_t server = NULL;
    ESP_ERROR_CHECK(httpd_start(&server, &config));
    const httpd_uri_t root = {
        .uri = "/", .method = HTTP_GET, .handler = root_handler, .user_ctx = NULL,
    };
    const httpd_uri_t update = {
        .uri = "/update", .method = HTTP_POST, .handler = update_handler, .user_ctx = NULL,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &root));
#ifndef BRIDGE_FLASH_READOUT
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &update));
#endif
    const httpd_uri_t status = {
        .uri = "/status", .method = HTTP_GET, .handler = status_handler, .user_ctx = NULL,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &status));
    const httpd_uri_t tasmota_settings = {
        .uri = "/tasmota-settings", .method = HTTP_POST,
        .handler = tasmota_settings_handler, .user_ctx = NULL,
    };
#ifndef BRIDGE_FLASH_READOUT
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &tasmota_settings));
#endif
#ifdef BRIDGE_NVS_READOUT
    const httpd_uri_t nvs = {
        .uri = "/nvs", .method = HTTP_GET, .handler = nvs_handler, .user_ctx = NULL,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &nvs));
#endif
#ifdef BRIDGE_FLASH_READOUT
    /* Deliberately unregistered in this build. */
    (void)update;
    (void)tasmota_settings;
#endif
    const httpd_uri_t flash = {
        .uri = "/flash", .method = HTTP_GET, .handler = flash_handler, .user_ctx = NULL,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &flash));
    const httpd_uri_t efuse = {
        .uri = "/efuse", .method = HTTP_GET, .handler = efuse_handler, .user_ctx = NULL,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &efuse));
    const httpd_uri_t flash_sha256 = {
        .uri = "/flash-sha256", .method = HTTP_GET, .handler = flash_sha256_handler, .user_ctx = NULL,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &flash_sha256));
    return ESP_OK;
}

void app_main(void)
{
    /* Vendor OTA may leave a newly booted bridge pending verification.  Confirm
     * it before selecting another slot, otherwise the bootloader can later
     * classify this recovery image as aborted.  A non-pending image can return
     * an error here; that is harmless and must not prevent recovery startup. */
    esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "bridge confirmation returned %s", esp_err_to_name(err));
    }

    s_running = esp_ota_get_running_partition();
    s_peer = esp_ota_get_next_update_partition(NULL);
    if (s_running == NULL || s_peer == NULL || s_running == s_peer ||
        s_peer->type != ESP_PARTITION_TYPE_APP) {
        ESP_LOGE(TAG, "No safe peer application partition; refusing to start");
        return;
    }

    /* Make the peer the automatic fallback for the next power cycle. */
    err = esp_ota_set_boot_partition(s_peer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Peer stock image is not bootable (%s); refusing to start",
                 esp_err_to_name(err));
        return;
    }
    ESP_LOGI(TAG, "power-cycle fallback selected: %s at 0x%08lx",
             s_peer->label, (unsigned long)s_peer->address);

    ESP_ERROR_CHECK(start_access_point());
    ESP_ERROR_CHECK(start_web_server());
    ESP_LOGI(TAG, "open http://192.168.4.1/ (password: %s)", BRIDGE_AP_PASSWORD);
}
