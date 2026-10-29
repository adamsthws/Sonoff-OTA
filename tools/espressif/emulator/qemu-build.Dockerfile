# Build environment for the lcgamboa QEMU fork (ESP32-C3 with Wi-Fi), so its
# build tooling stays out of the host. Ubuntu 24.04 matches Linux Mint 22, so
# the binary it builds also runs on the host.
#
#   docker build -t autoflash-qemu-build -f tools/espressif/emulator/qemu-build.Dockerfile tools/espressif/emulator
#   docker run --rm -u "$(id -u):$(id -g)" -v ~/Git/emu/lcgamboa-qemu:/src autoflash-qemu-build
FROM ubuntu:24.04

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential ninja-build pkg-config python3 python3-venv git ca-certificates \
        libglib2.0-dev libpixman-1-dev libslirp-dev libgcrypt20-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# Only the RISC-V system emulator; the shared-library step of the fork's own
# build script is for PICSimLab and not needed here.
CMD ["sh", "-c", "mkdir -p build && cd build && ../configure --target-list=riscv32-softmmu \
     --enable-slirp --enable-gcrypt --disable-werror --disable-docs --disable-gtk --disable-sdl \
     --disable-opengl --disable-vnc && ninja qemu-system-riscv32"]
