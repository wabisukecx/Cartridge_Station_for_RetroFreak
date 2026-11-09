# GB/GBC read design

[日本語](ja/SYSTEM_GB_GBC.md)

## Scope and assumptions

- Game Boy and Game Boy Color (GBC) cartridges. They go into the reader's slot shared by GB/GBC/GBA. The cartridge state code is 5 (common to GB and GBC).
- Only the ROM area is read. It does not enable, read, or write save RAM, and does not operate RTC, infrared, the camera's capture registers, EEPROM, the tilt sensor, or flash.
- GB and GBC are told apart by the header's CGB flag (0x80/0xC0 means GBC). The output extension is .gb or .gbc.

## Identification

1. Start the mode (04(1), 05, a 200 ms wait).
2. Read the header (80 bytes from 0x100) two or more times and confirm they match.
3. Verified items: the Nintendo logo (48 bytes), the header checksum, the ROM size code (00–08), and the cartridge type.
4. The screen shows the title in the header, the MBC, the ROM capacity, and the GB/GBC class. The official name in the DB is fixed after the full-ROM hash is matched.
5. Run the shutdown step.
- If the logo or checksum does not match, or the header read is unstable, it stops. It does not import with the checks turned off. It does not hard-code the MBC or the system.
- The global checksum alone never confirms an official name or a successful read. The DB hash of the whole finished ROM is mandatory.

## Reading

1. Re-check the header when the import starts.
2. Run two independent full-ROM reads. For each bank it writes the MBC registers and reads 16 KiB at a time from the ROM window (the first bank from the fixed window at 0x0000, the rest from the switchable window at 0x4000).
3. Confirm that the two reads match, the header matches, and the DB's SHA-1 (and SHA-256, if recorded) matches.
4. After the shutdown step, read the header again and confirm the cartridge has not changed.
- The capacity is decided by the header's size code (doubling from 32 KiB) and checked against the DB's size.
- A method that failed is not retried unconditionally with another method. A communication error, cancellation, a mismatch between the two reads, a header change, or a SHA-256 mismatch does not lead to trying another method.

## Supported MBCs

| Method decided from the header | Applies to | Registers operated and notes |
|---|---|---|
| ROM only (type 00) | 32 KiB | No bank switching |
| MBC1 | Up to 2 MiB | 0x2000 (ROM number), 0x4000 (upper 2 bits), 0x6000 (mode). Banks 32, 64, and 96 are read from the lower window in mode 1 (the upper window would read the next bank instead) |
| MBC1M-equivalent (Ex2M/Ex4M) | Up to 2 MiB | A read for the special wiring. At 1 MiB it chooses a candidate by comparing bank contents. The comparison can misjudge by coincidence, so its result is not used as grounds for saving; it is confirmed by a DB match of the finished ROM |
| MBC2 | Up to 256 KiB | ROM number only |
| MBC3 | Up to 2 MiB | ROM number only. RTC is not operated |
| MBC5 | Up to 8 MiB | A 9-bit ROM number (0x2000 and 0x3000) |
| MBC6 (type 0x20) | Up to 1 MiB | Selects ROM for both of the independent 8 KiB windows A and B (writing 0 to 0x2800 and 0x3800), sets two consecutive banks with the 7-bit numbers at 0x2000 and 0x3000, and reads 16 KiB at a time. RAM and flash are out of scope |
| MBC7 (type 0x22) | Up to 2 MiB | A 7-bit ROM number (0x2000). The MBC5-compatible write of 0 to 0x3000 is not done (it would overwrite the ROM selection). EEPROM and the tilt sensor are not operated |
| HuC1 (type 0xFF) | Up to 1 MiB | A 6-bit ROM number (0x2100). 0x4000 selects RAM bank 0 and is not used as an upper ROM bit |
| HuC3 (type 0xFE) | Up to 2 MiB | A 7-bit ROM number. 0x4000 selects RAM. RAM, RTC, and infrared are not accessed |
| TAMA5 (type 0xFD) | Up to 512 KiB | Through a dedicated entry point, it writes 0xA001=1, 0xA000=number÷16, 0xA001=0, 0xA000=number%16 in that order (1 upper bit plus 4 lower bits). An arbitrary write to 0xA000 is not exposed. Saves, RTC, and the alarm are out of scope |
| Pocket Camera (type 0xFC) | Up to 1 MiB | ROM number only (0x2100). Capture, photos, and the camera registers are not accessed |

- The MBC1 normal wiring's banks 32, 64, and 96 would be wrong with a normal bank read as it is, so a correction using the lower window in mode 1 is included.
- Not supported: multicarts such as MMM01 and GB Memory; TAMA5 beyond 512 KiB; MBC6 beyond 1 MiB and its flash area; HuC1 and Pocket Camera beyond 1 MiB.
- HuC1 is limited to 1 MiB, which a 6-bit ROM number can express. MBC6 is limited to 1 MiB because its bank numbers are 7 bits in units of 8 KiB, so the numbering wraps back to the start at the 64th 16 KiB block. A header whose capacity exceeds this stops before any communication starts.
- The entry point for writing bank registers (gb_register) restricts addresses and values. MBC6 and TAMA5 have dedicated entry points.

## Matching and saving

- The DB is a linear-ROM DB with titles, sizes, and SHA-1 (optionally SHA-256), about 3,455 entries. Records whose size is out of range are not included.
- The GB/GBC classification is matched in full under the classification decided from the header. If it does not match, the other classification is checked, and if the same capacity and SHA-1 (and SHA-256, if recorded) exist in the other classification, it stops with "the header's classification and the DB's classification differ" (it does not save the ROM and does not reclassify). For example: `GB/GBC classification differs: header GBC, DB GB; no ROM saved. SHA-1: ...`
- If both classifications have the same ROM, an exact match with the header's classification wins and the ROM is saved as usual.
- It is saved under the DB's official name as .gb or .gbc.

## Save folder and MiSTer core

| Key | PC default | MiSTer default | Core |
|---|---|---|---|
| GB | ~/RetroFreakDumps | /media/fat/games/Gameboy | _Console/Gameboy, type=f, index=1, delay=2 |
| GBC | ~/RetroFreakDumps | /media/fat/games/GBC | Same as above |

- The reason GB and GBC have separate save folders: MiSTer's ConsoleMode does not recognize GBC ROMs in the Gameboy folder, but does in the GBC folder. The core itself is the shared Gameboy core.
- The save folder and the core can be changed separately with the GB and GBC keys in the settings file. If a key is missing, the defaults above are filled in.
- The GUI's Custom DB is for FC; GB/GBC use the bundled DB. The CLI can specify a different DB with `--gb-db`.

## Diagnostics

- The diagnostic command (diagnose-gb) reads the GB header (80 bytes from 0x100) three times, and prints as JSON the received hex, the number of bytes matching the logo (out of 48), the checksum, the flag, type, and capacity code, and whether the reads match each other. It starts and ends the reader's read mode, but does no bank writes, no whole-ROM read, and no saving.
- Additional options:
  - A specification for comparison that sends the connection-start 04(0) before getting the product information. It is not applied automatically to a normal import.
  - Output of a communication trace (JSON Lines). It is used to compare sends and receives between environments.
- Narrowing down symptoms: if only a particular cartridge fails to match across the two header reads, or its logo does not match, the cause is one of the contacts, the reader's state, or the receive handling, and the diagnostic output is used to compare. Do not work around it by turning the checks off.

## What verification means

- For each MBC, with a synthetic ROM that independently imitates the bank decoding, the tests confirm reconstruction of every bank, the top bank, corruption in the second read, an unknown DB entry, communication failure, cancellation, detection of a swap, and the full verification and reuse after saving.
- The addresses, sizes, and write order are compared with the reference traces for the normal ROM and MBC1 and others. Corrections that are not in the reference traces (MBC1's banks 32, 64, and 96; HuC3's 7 bits; MBC7's omitted write to 0x3000; and the limits of MBC6 and HuC1) are verified with independent models based on specification documents.
- For methods without a real cartridge, verification stops at the model.
