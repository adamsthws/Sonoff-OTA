idf_component_register(SRCS "main.c" "tasmota_settings.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_http_server esp_wifi app_update esp_partition nvs_flash esp_event esp_netif mbedtls spi_flash)
