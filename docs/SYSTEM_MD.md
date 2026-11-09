# Mega Drive (MD) read design

[日本語](ja/SYSTEM_MD.md)

## Scope and assumptions

- Mega Drive / Genesis cartridges. The cartridge state code is 2.
- Only the ROM is read. No data is written to save RAM, EEPROM, or Flash.
- The only writes (command 0x0E) are to the ROM mapping registers 0xA130F1 (value 0/1) and 0xA130FF (value 0–23). No other address is exposed.

## Communication

- Start is 04(1), 05, and a 200 ms wait. End is 04(0), 05.
- The read command is 0x0D. Addresses are in bytes, in blocks of up to 32 KiB. After checking the result header, it sends the state request, then receives the data and the state reply.

## Identification

1. Read 0x100–0x1FF 16 bytes at a time, and require two independent reads to match.
2. Check the SEGA identifier, and parse the ROM start and end addresses (big-endian).
3. Obtain the title, serial, read method, and a candidate capacity. This capacity is a candidate from the header and similar information, not a capacity confirmed against the DB.
- A byte-swapped SEGA identifier (ESAG and the like) is an explicit error and is not let through by automatic conversion.
- Shift-JIS conversion of the title falls back to an ASCII substitute in an environment without the codec, and does not affect the byte sequences or hashes used for decisions.

## Read methods

| Method | Range and operation | Selection |
|---|---|---|
| None | A normal linear ROM, up to 4 MiB | Normal automatic decision |
| SuperSf2 | 5 MiB when automatic. After the normal area, it switches a 512 KiB window with 0xA130FF. The implementation limit is 12 MiB. On exit it restores 0xA130F1=0 and the read window to 0xA130FF=7 | Decided automatically by the domestic title identifier (32 bytes) |
| SAndN | A lock-on configuration (the main cartridge plus an attached cartridge). It compares the extra header at 0x200100 and adjusts the capacity and switching to the attached configuration | Decided automatically by the domestic title identifier and the attached title |
| MortalK2 | Up to 4 MiB. From 2 MiB onward it reads at a different address, and re-reads a reply that contains FF a limited number of times / switches to 2-byte units. A remainder of less than 64 bytes is not missed either | Not included in the automatic decision; specified by hand on the CLI |

- Normally the capacity follows the header's ROM range and is never decided from the title name alone. For known exceptions where the capacity does not match the header, the capacity is corrected only when both the title and the serial match.
- Before each independent read, the necessary mapping is set. On exit or interruption, it tries to release it.
- A missing header, an invalid range, an identifier other than SEGA, and an unknown bank control stop as unsupported. A DB size that has no read method cannot be imported.

## Reading and verification

1. After the user's confirmation, read the whole ROM twice independently from address 0.
2. Require an exact match, a header match, and the DB's full SHA-1 (and SHA-256, if recorded). The ROM's 16-bit additive checksum is not a required condition.
3. An unknown full-ROM hash is not saved.
- The DB has 1,969 entries (512 bytes to 16 MiB, even length). It also contains sizes and special boards that the methods above cannot read. Being listed does not mean supported.

## Saving

- The received bytes are saved as they are (no 16-bit swap, no SMD interleave, no 512-byte header added). The extension is .md. The official name is fixed after the DB match.
- A file with the same name and different content is left in place, and the verified data is saved under an alternate name.

## Save folder and MiSTer core

| Environment | Default save folder | Core |
|---|---|---|
| Windows | ~/RetroFreakDumps | Not launched automatically |
| MiSTer | /media/fat/games/MegaDrive | _Console/MegaDrive, type=f, index=1, delay=2 |

- The save folder and the core can be changed with the MD key in the settings file.
- MiSTer's MD core has a region decision by file extension (.md is Europe), so for a Japanese version set the core's Auto Region to Header or choose Japan. The region and the byte sequence of the saved ROM itself are not changed.

## CLI

- `identify-md`: checks the header. It prints state, system, title, serial, controller, rom_size, and readable. It does not save.
- `dump-md`: imports with confirmation. `--md-db` specifies a different DB, and `--controller` specifies the method by hand (None, SAndN, SuperSf2, MortalK2).

## What verification means

- The bank reads of the four methods are compared with the reference traces, recorded on a synthetic bus, for the Python read addresses, sizes, and bank writes.
- The places that intentionally differ from the reference (the remainder handling of the MortalK2 method, and the restoration of the SuperSf2 method's read window) are verified with the difference stated explicitly.
- The special methods are verified only by synthetic tests; their operation on real special boards is not guaranteed.
