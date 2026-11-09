#!/bin/sh
# Read-only diagnostics. Does not open the reader or alter drivers.
printf '=== System ===\n'
uname -a
printf '\n=== Runtimes ===\n'
for name in python3 lsusb; do
    command -v "$name" || true
done
if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys, importlib.util; print(sys.version); print("pyserial:", bool(importlib.util.find_spec("serial"))); print("pyusb:", bool(importlib.util.find_spec("usb")))'
fi
printf '\n=== RetroFreak USB descriptors ===\n'
if command -v lsusb >/dev/null 2>&1; then
    lsusb -d f00d:1337 -v
fi
printf '\n=== RetroFreak sysfs ===\n'
for device in /sys/bus/usb/devices/*; do
    [ -r "$device/idVendor" ] && [ -r "$device/idProduct" ] || continue
    [ "$(cat "$device/idVendor")" = f00d ] || continue
    [ "$(cat "$device/idProduct")" = 1337 ] || continue
    printf 'Device: %s\n' "$device"
    for field in manufacturer product bDeviceClass bNumConfigurations; do
        if [ -r "$device/$field" ]; then
            printf '%s: ' "$field"
            cat "$device/$field"
        fi
    done
    for interface in "$device":*; do
        [ -d "$interface" ] || continue
        printf 'Interface: %s\n' "$interface"
        [ ! -L "$interface/driver" ] || readlink "$interface/driver"
    done
done
printf '\n=== Serial nodes ===\n'
for device in /dev/ttyACM* /dev/ttyUSB*; do
    [ ! -e "$device" ] || ls -l "$device"
done
printf '\nDiagnostics complete. No reader commands were sent.\n'
