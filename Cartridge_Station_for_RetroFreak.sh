#!/bin/sh
# Cartridge Station for RetroFreak / MiSTer FPGA Edition.
# Prefer the Scripts installation; otherwise run from this script's folder.
scripts=/media/fat/Scripts
if [ -f "$scripts/Cartridge_Station_for_RetroFreak/cart_station.py" ]; then
    cd "$scripts/Cartridge_Station_for_RetroFreak" || exit 1
else
    directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
    if [ ! -f "$directory/cart_station.py" ]; then
        printf '%s\n' 'Cartridge Station for RetroFreak installation not found.' >&2
        exit 1
    fi
    cd "$directory" || exit 1
fi
export TERM=linux
exec python3 ./cart_station.py manager "$@"
