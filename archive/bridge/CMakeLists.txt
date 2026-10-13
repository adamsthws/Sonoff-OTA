cmake_minimum_required(VERSION 3.16.0)

# The bridge's version, as main.c's BRIDGE_VERSION and the wrapper carry it.
set(PROJECT_VER "4.4.2")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(s60_ota_bridge)
