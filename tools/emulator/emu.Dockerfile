# A virtual Sonoff S60 and the computer that converts it, in one container.
#
# The container is "your computer on the home Wi-Fi": it runs the emulated plug
# (lcgamboa's QEMU fork, which emulates the ESP32-C3's Wi-Fi), a small router
# for the emulated home network, and the conversion tool. Start it with
# tools/emulator/emu.sh, never by hand - that script prepares the private
# state it needs under captures/emulation/.

# -- build the QEMU fork, pinned, with this project's patches --------------------
FROM ubuntu:24.04 AS qemu
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential ninja-build pkg-config python3 python3-venv git ca-certificates \
        libglib2.0-dev libpixman-1-dev libslirp-dev libgcrypt20-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
# The fork's source at its pinned commit (lcgamboa/qemu e39508f), with the
# subprojects its build would otherwise fetch, published with this repository
# (emu.sh downloads it and checks it): nothing else is needed to build it, and
# it is built with no network at all.
ARG QEMU_SOURCE_SHA256=863c653128bd9dafd353e5321d5e31e00337dfd42346ee30e8d496eadaaf3b82
COPY qemu-source.tar.xz /tmp/
RUN echo "$QEMU_SOURCE_SHA256  /tmp/qemu-source.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/qemu-source.tar.xz -C / && mv /qemu /src && rm /tmp/qemu-source.tar.xz
COPY fork/ /patches/
RUN --network=none cd /src && for p in /patches/*.patch; do git apply "$p"; done \
    && mkdir build && cd build \
    && ../configure --target-list=riscv32-softmmu --enable-slirp --enable-gcrypt \
         --disable-werror --disable-docs --disable-gtk --disable-sdl --disable-opengl --disable-vnc \
    && ninja qemu-system-riscv32 \
    && mkdir -p /opt/qemu/bin /opt/qemu/pc-bios \
    && cp qemu-system-riscv32 /opt/qemu/bin/ && cp ../pc-bios/esp32c3-rom.bin /opt/qemu/pc-bios/

# -- runtime ---------------------------------------------------------------------
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libglib2.0-0t64 libpixman-1-0 libslirp0 libgcrypt20 zlib1g \
        python3 python3-venv iproute2 iptables dnsmasq-base udhcpc iputils-ping \
        tmux socat tcpdump curl sudo procps less ca-certificates ufw \
    && rm -rf /var/lib/apt/lists/*
COPY --from=qemu /opt/qemu /opt/qemu
# The tool needs only Python's standard library; the venv just provides `python`.
RUN python3 -m venv /opt/venv
COPY container/ /opt/autoflash-emu/
RUN chmod 0755 /opt/autoflash-emu/bin/* /opt/autoflash-emu/entrypoint.sh /opt/autoflash-emu/udhcpc.script \
    && ln -s /opt/autoflash-emu/bin/* /usr/local/bin/
ENV PATH=/opt/venv/bin:$PATH
ENTRYPOINT ["/opt/autoflash-emu/entrypoint.sh"]
