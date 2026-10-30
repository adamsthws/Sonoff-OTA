# Where OpenBeken is built for the S61 (tools/repo/update_firmware.py openbeken):
# Ubuntu 24.04 with what OpenBK7231T_App's Makefile needs. The Beken SDK brings
# its own arm-none-eabi-gcc, which wants GLIBC 2.34 or newer - OpenBeken's own
# build image is too old for it - and its pack step calls `python`.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        build-essential cmake git make python3 python-is-python3 srecord wget xxd xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git config --system --add safe.directory '*'
WORKDIR /app
