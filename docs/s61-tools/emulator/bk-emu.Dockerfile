# The Sonoff S61's emulator in one container: BekenEmulator (openshwprojects),
# unmodified upstream at the commit UPSTREAM pins, with this project's patches
# applied, and the Python it imports. Build it with bk-emu.sh, never by hand:
# that script downloads the source archive, checks it and labels the image.
# No firmware is in the image: dumps are fetched at run time (bk-emu.sh dump)
# and mounted in at /opt/beken-emulator/firmwares.
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-venv patch xz-utils openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# The archive's hash, as UPSTREAM pins it; bk-emu.sh passes it.
ARG SOURCE_SHA256
COPY bekenemulator.tar.xz /tmp/
RUN echo "$SOURCE_SHA256  /tmp/bekenemulator.tar.xz" | sha256sum -c - \
    && mkdir /opt/beken-emulator \
    && tar -xJf /tmp/bekenemulator.tar.xz -C /opt/beken-emulator --strip-components=1 \
    && rm /tmp/bekenemulator.tar.xz
COPY patches/ /patches/
RUN cd /opt/beken-emulator && for p in /patches/*.patch; do patch -s -p1 < "$p"; done \
    && mkdir -p firmwares report && chmod 0777 report
COPY requirements.txt /tmp/
RUN python3 -m venv /opt/venv && /opt/venv/bin/python -m pip install -q -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
ENV PATH=/opt/venv/bin:$PATH BK_EMU_ROOT=/opt/beken-emulator PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
ENTRYPOINT ["python"]
