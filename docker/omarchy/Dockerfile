# Omarchy in a Docker container via QEMU.
#
# Adapted from domarchy (https://github.com/overthinkinglabs/domarchy),
# MIT License, Copyright (c) 2025 overthinkinglabs.
#
# The guest is an emulated x86_64 VM, so this image runs on both amd64 and
# arm64 (Apple Silicon) Docker hosts. On amd64 hosts with KVM the VM runs
# near-native; elsewhere QEMU falls back to TCG emulation (slower).

FROM archlinux:latest

RUN pacman -Syu --noconfirm --needed \
        qemu-system-x86 \
        qemu-img \
        edk2-ovmf \
        wget \
        samba \
        python-pip && \
    pip install --break-system-packages --no-cache-dir websockify && \
    wget -qO- https://github.com/novnc/noVNC/archive/refs/tags/v1.6.0.tar.gz | tar xz -C /opt && \
    mv /opt/noVNC-1.6.0 /opt/novnc && \
    cp /opt/novnc/vnc.html /opt/novnc/index.html && \
    printf '{"autoconnect":true,"resize":"scale","path":"websockify"}\n' > /opt/novnc/defaults.json

EXPOSE 5900 8900

COPY run-vm.sh /run-vm.sh
RUN chmod +x /run-vm.sh

CMD ["/run-vm.sh"]
