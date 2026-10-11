#!/bin/bash
# Boots the Omarchy guest VM. Adapted from domarchy (MIT, overthinkinglabs).
#
# First run: downloads the Omarchy ISO, creates a qcow2 disk, and boots the
# installer. Complete the installer in the noVNC display; subsequent starts
# boot the installed system directly.

set -e

DISK=/data/omarchy.qcow2
ISO=/data/omarchy.iso
MEMORY=${MEMORY:-8G}
CPUS=${CPUS:-4}
OMARCHY_ISO_URL=${OMARCHY_ISO_URL:-https://iso.omarchy.org/omarchy-4.0.0.iso}

if [[ ! -f "$ISO" ]]; then
    echo "Downloading Omarchy ISO..."
    wget -O "$ISO" "$OMARCHY_ISO_URL"
fi

if [[ ! -f "$DISK" ]]; then
    echo "Creating disk image..."
    qemu-img create -f qcow2 "$DISK" 50G
    CDROM_OPTION="-cdrom $ISO"
    BOOT_ORDER="dc"
else
    echo "Starting Omarchy..."
    CDROM_OPTION=""
    BOOT_ORDER="d"
fi

websockify --web /opt/novnc 8900 localhost:5900 &

exec qemu-system-x86_64 \
    -m "$MEMORY" -smp "$CPUS" -machine q35,accel=kvm:tcg \
    -drive file="$DISK",format=qcow2 \
    $CDROM_OPTION \
    -boot order=$BOOT_ORDER \
    -display vnc=:0 \
    -device VGA,edid=on,xres=1920,yres=1080,vgamem_mb=32 \
    -audiodev none,id=audio0 \
    -net user,smb=/shared \
    -net nic
