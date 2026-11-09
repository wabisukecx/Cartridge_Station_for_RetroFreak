# PC Engine / SuperGrafx (HuCard) read design

[日本語](ja/SYSTEM_PCE.md)

## Scope and assumptions

- HuCards for the PC Engine (TurboGrafx-16) and SuperGrafx. The cartridge state code is 8.
- Only the ROM is read. CD-ROM, Tennokoe Bank switching, backup RAM, and Flash are out of scope.
- The only writes (command 0x2E) send the value 255 to 0x1FF0–0x1FFF, for Street Fighter II.

## Communication

- Start is 04(1), 05, and a 200 ms wait (5 V by default). It has no processing that switches the voltage by probing. End is 04(0), 05.
- The read command is 0x2D. Addresses are in bytes, up to 32 KiB.
- Unlike the other systems, it does not insert a state request in the middle of receiving the data. After receiving the reply header and all the data, it does an independent state check.

## Identification (reads the whole ROM on insertion)

A HuCard has no common title/capacity header like GB or MD. So on insertion the tool decides the method, matches the full-ROM hash against the DB with one whole read, and shows the official name and capacity on the screen.
- This identification does not save. Saving happens after the confirmation of Read/DUMP, through two independent reads.
- The same insertion is not identified repeatedly while waiting. Removing and reinserting, reconnecting, and retrying read it again.
- If identification fails, mismatches, or is interrupted, nothing is saved, and the save folder is not created. Progress is shown during identification, and it can be cancelled.
- The diagnostic that only examines the method (probe-pce) does not do a whole read or DB matching.

### Deciding the method

- Reads 8 KiB each at 0, 0x40000, and 0x80000 and compares them; when needed, it checks whether the content at 0x80000 changes with 0x1FF1=255. Each sample must match across two reads, and the switch is restored with 0x1FF0=255.

| Method | Read | Limit |
|---|---|---|
| Linear | Contiguous addresses | 1 MiB |
| Interval | Reads logical 0x40000 and later from physical 0x80000 | 512 KiB |
| Sf2Dash | The first 1 MiB is bank 0. After that, a 512 KiB window is switched | 8.5 MiB |

- The method is decided automatically. To specify it by hand, use `--controller` (Linear, Interval, Sf2Dash). On a mismatch, another method is not retried unconditionally.

### Capacity and data order

- The final capacity is decided not by guessing from mirrors or FF regions but by the full SHA-1 at each size recorded in the DB. If nothing matches, it reads up to the method's limit and stops, and an unknown ROM is not saved.
- The PC Engine and the TurboGrafx wire the data lines differently. It compares two full-ROM hashes against the DB, one in the received order and one with the bits of each byte reversed, and decides the data order by which one matches. The decision uses only the full-ROM hash; it does not convert by a heuristic on the last byte.
- The first DB match fixes the capacity, official name, data order, and the PCE/SGX class. It then reads that length again from the beginning independently, and confirms that the two raw reads match, that the first sample matches, and that the normalized full SHA-1 (and SHA-256, if recorded) matches before saving.

## DB and extensions

- 409 entries: 404 for PCE/TurboGrafx and 5 for SuperGrafx. No ROM content is included.
- SuperGrafx is classified by the DB's full-ROM hash, not decided from the title before reading. The extension is .pce for PCE and .sgx for SuperGrafx.

## Save folder and MiSTer core

| System | Save folder (PC) | Save folder (MiSTer) | Launch |
|---|---|---|---|
| PCE | ~/RetroFreakDumps | /media/fat/games/TGFX16 | _Console/TurboGrafx16, type=f, index=0, delay=2 |
| SGX | ~/RetroFreakDumps | /media/fat/games/TGFX16 | The same core, type=f, index=1, delay=2 |

- The core's launch index differs between PCE and SGX (0 for PCE, 1 for SGX). Keep this index even when you change the core or delay with the PCE/SGX keys in the settings. A different index is rejected by validation of the settings.
- If a save folder dedicated to SGX is configured, the ROM is saved there after the DB classification.

## CLI

- `probe-pce`: examines only the method and the first sample (state=sample_only).
- `identify-pce`: does one whole read and DB matching, and shows state=identified, the DB's official name, rom_size, and data_order. It does not save.
- `dump-pce`: imports with confirmation. `--pce-db` specifies a different DB.

## What verification means

- The bank reads of the three methods are compared with the reference traces, recorded on a synthetic bus, for addresses and bank writes.
- With synthetic data, the tests verify that no state request is sent before reception completes, the method decision, Interval's 384 KiB, Sf2Dash's windows, bit reversal, two independent reads, unknown, ambiguous, and SHA-256-mismatch cases, interruption, change, and disconnection, reuse of a save, the SGX classification and save folder, and the PCE/SGX MGL index.
- The special methods and SuperGrafx are verified only by models unless there are tests using real hardware.
