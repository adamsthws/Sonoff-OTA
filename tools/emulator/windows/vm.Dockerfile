# A Windows VM in a container, for tools/emulator/windows-test.sh: Windows
# Server 2025 Server Core, installed from Microsoft's evaluation ISO and run by
# QEMU with KVM. The container is the VM's only way out: user-mode networking,
# its screen on VNC, Remote Desktop forwarded, nothing else. It builds Setup's
# seed disc (genisoimage) and reads and writes the disk between runs
# (libguestfs).
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        qemu-system-x86 qemu-utils ovmf libguestfs-tools linux-image-kvm genisoimage \
        python3 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# libguestfs boots its own small appliance from the kernel installed above.
ENV LIBGUESTFS_BACKEND=direct
COPY guest/ /opt/s60win/guest/
COPY vm-run.sh /usr/local/bin/vm-run
RUN chmod 0755 /usr/local/bin/vm-run
ENTRYPOINT ["/usr/local/bin/vm-run"]
