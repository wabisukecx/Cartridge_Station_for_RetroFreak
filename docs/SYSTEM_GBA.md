# GBA read design

[日本語](ja/SYSTEM_GBA.md)

## Scope and assumptions

- Game Boy Advance cartridges. They go into the reader's slot shared by GB/GBC/GBA. The cartridge state code is 6.
- Only the ROM is read. It does not read or write SRAM, EEPROM, or Flash, does not control RTC or GPIO, and does no special bank switching.
- The output is .gba. It is saved under the official name recorded in the DB.

## Communication

- The read command is 0x1F. Addresses are in bytes. One read is a block of up to 32 KiB.
- After checking the reply header, it sends the state request, then receives the data and the state reply.
- Start is 04(0), 05, and a 200 ms wait. End is 04(0), 05.

## Identification

1. Read the first 2 KiB twice independently and confirm they match.
2. Verify the fixed value (0x96) in the first 192 bytes and the header checksum. It does not match the whole 156-byte logo. A passing header check alone is not treated as proof of an authentic ROM or as a DB match.
3. What the header gives is the title string, the game code, and the revision. The ROM capacity is not in the header, so the capacity at identification is shown as "Auto (DB)".

## Fixing the capacity and reading

1. After the import is confirmed, the first read checks the full SHA-1 at each size recorded in the DB, and fixes the capacity and official name where it matches exactly.
2. It reruns 04(0), 05, and the 200 ms wait, and confirms the cartridge state and the reception of a complete first 2 KiB with a matching header. It then reads the fixed length again from address 0. This is not a power cycle.
3. It saves only after confirming that the two reads match and the DB's SHA-1 (and SHA-256, if recorded) match.
- The capacity decision is matched not only at 1/2/4/8/16/32 MiB but at every size in the DB (including small capacities and lengths that are not powers of two). The capacity is never shortened by detecting an FF region or a mirror alone.
- If nothing in the DB matches even after reading up to 32 MiB, it stops, and an unknown ROM is not saved.
- Until the capacity is fixed, the progress percentage is based on the upper limit of 32 MiB, so the display may jump forward when the second read starts.
- The DB has 3,154 entries (192 bytes to 32 MiB). Being listed does not mean every board was confirmed on hardware.

## Save folder and MiSTer core

| Environment | Default save folder | Launch |
|---|---|---|
| Windows | ~/RetroFreakDumps | Use from the save folder |
| MiSTer | /media/fat/games/GBA | _Console/GBA, type=f, index=1, delay=2 |

- Before launching, it checks again that the core's RBF exists and the saved ROM's full-ROM hash.
- The GBA core may need a BIOS to be placed, and that placement is the responsibility of the user's environment.
- The save folder and the core can be changed with the GBA key in the settings file.

## CLI

- `identify-gba`: checks the header (does not save a ROM). The output is state (header_only), system, title, game_code, and revision. `rom_size` being null is the normal display for "capacity not yet fixed".
- `dump-gba`: imports with confirmation. `--gba-db` specifies a different DB.

## What verification means

- With synthetic data, the tests verify the header, communication frames, the 32 MiB boundary, DB matching of small capacities and lengths that are not powers of two, preservation of FF regions, two independent reads, mismatches, disconnection, interruption, reuse of a save, detection of tampering, the MGL, the CLI, and both screens' displays.
- The read addresses and sizes are compared with the reference traces.
