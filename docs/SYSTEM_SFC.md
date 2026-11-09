# SFC (Super Famicom) read design

[日本語](ja/SYSTEM_SFC.md)

## Scope and assumptions

- Super Famicom cartridges. The cartridge state code is 1.
- Only the ROM is read. Save data, RTC, and flash are not operated.
- Cartridges with a SuperFX are handled by the normal LoROM ROM read. The SuperFX processing during the game is done by the MiSTer core, and the success of a read and the game's behavior are treated as separate facts.
- ROM reads for dedicated methods: SA-1 and S-DD1 up to 8 MiB. CX4 is limited to the LoROM (up to 2 MiB) of the one title permitted by the title in the header. These have been confirmed only on a synthetic bus, and success on hardware has not been confirmed.
- Not supported: SPC7110, CX4 other than the one permitted title, SF Memory, Sufami Turbo, and add-on slots. The ROM initialization of SPC7110 includes operating a save-RAM control register (0x4830), which cannot be handled under the policy of not operating save areas, so it is unsupported.

## Communication

- The read command is 0x07. A 24-bit byte address, and one read is 1–32768 bytes. After the reply header it sends the state request, and after receiving the data it confirms state code 1.
- Initialization: 04(1), 2F(1), 05, 05, then read 16 KiB from $00:C000.
- End: 2F(0), 04(0), 05.
- A 0x2F packet has five words, and the value is the last word. No extra checking is done by guessing the meaning of the reply (ACK).
- ROM register writes use 0x08. The request is five words `(8, 8, 1, address, 1)`, then the ready reply `(0, 0)`, the 1-byte value, and the completion reply `(0, 0)`, in that order. The public API allows only the ROM addresses and values below.

| Method | Writes allowed | ROM read |
|---|---|---|
| SA-1 | 0–3 to each of 0x2220–0x2223, and 4–7 for the second 4 MiB | Switched every 128 banks. The first 64 banks start at 0x008000 and the last 64 banks at 0x808000, at 64 KiB intervals |
| S-DD1 | 0–3 to each of 0x4804–0x4807, and 4–7 for the second 4 MiB | From 0xC00000 at 32 KiB intervals. The window is switched every 128 banks |
| CX4 (the one permitted title) | Only 0 to 0x7F52 | A normal LoROM. It is set after a stable check of the current header, and the warm-up and the header match are checked again |

Each register accepts only the values that belong to that register (for example, 0x2220 takes 0 or 4). Enabling save RAM and writing to an arbitrary address are not exposed. For the dedicated methods, the setup is done at the start of each pass and before each bank. The design decisions are collected in "Design decisions for the dedicated methods" below.

## Design decisions for the dedicated methods

- For SA-1 and S-DD1, the registers in the table above are written in a fixed order at the start and for each bank, and then the data is read. The header of the finished ROM is verified at 0x7FB0.
- Writes are limited, individually, to ROM registers and values. It never writes SA-1's save-write permission or S-DD1's DMA control. The write entry point's public API rejects forbidden addresses, forbidden values, and wrong types.
- For CX4, it sends 0x7F52=0 only for the target title. It confirms the current cartridge's stable header, writes only after it matches the selected profile, and then does the warm-up and a re-check of the header. A dedicated write is never sent to an unconfirmed cartridge.
- The 2 MiB cap for CX4 is a conservative limit of this implementation, not the capacity limit of the chip as a whole.
- The hardware decision uses the title inside the 48-byte real header, not the display name after DB matching. The decision does not change when the name becomes the DB's official name.
- For the headers of SA-1 and S-DD1, the combination with the chip type is limited as well as the map value.
- At the end of each pass, it tries the normal mode shutdown. Restoring all of the dedicated registers' state, or a power cycle, is not guaranteed.

## Identification (header only)

- On connection it only recognizes the header (unlike PCE, it does not read the whole ROM on insertion).
- It reads 48 bytes from $00:FFB0 twice, and checks: the two reads match, the XOR of the checksum and its complement is FFFF, a known map value, and the capacity code.
- Shift-JIS is used only to display the title. In an environment without the codec, an ASCII substitute is used, and the byte sequences used for decisions and the ROM hashes do not change.

### Limited retry for an invalid header

- At identification and at the header re-check when an import starts, only if two complete 48-byte reads match and the content of that header alone is invalid, it does the shutdown step, a 100 ms wait, and re-initialization.
- The total attempts are at most 5 including the first (at most 4 retries). The screen shows progress such as "Checking SFC header (2/5)", and the CLI prints the count to standard error (not mixed into the JSON output).
- What is not retried: communication loss, a short reply, a mismatch between the two header reads, cancellation, a removal or a different system, a failure of the shutdown step, a valid but unsupported chip, a swapped cartridge, and a mismatch of the whole ROM.
- If it has not recovered in 5 attempts, it stops, showing the number of attempts and the last reason.
- The retry treats 5 total attempts as a strict upper bound, and distinguishes invalid content from communication failure.

## Read methods

- LoROM, HiROM, BigLoROM, and LoROM+HiROM are up to 4 MiB. ExHiROM is up to 8 MiB.
- LoROM's last 2 banks and HiROM's last 4 banks use the FE/FF area.
- A LoROM larger than 2 MiB gets an additional map decision. If the decision samples are unstable, it stops.
- The capacity in the header is treated as a candidate upper bound. The capacity is fixed when the full SHA-1 (and SHA-256, if recorded) matches at a size boundary recorded in the DB. A capacity that is not a power of two can also be fixed by the DB.

## Reading and verification

1. After the import is confirmed, re-check the header and the method.
2. Read the whole ROM and fix the capacity with the DB. Then do the shutdown step, a state check, re-initialization, and a re-check of the stable header and method, and read the same ROM again from the beginning. An invalid header just before the second read is not retried.
3. Verify with the match of the two reads and the DB's full-ROM hash.
4. Check the saved file again.

## Saving

- The output is .sfc with no copier header. The official name is fixed after the full DB match.
- A file with the same name and different content is left in place, and the verified data is saved under an alternate name. An existing identical ROM is reused.
- The DB has 3,693 entries (excluding 10 that are under 32 KiB or over 16 MiB).

## Save folder and MiSTer core

| Environment | Default save folder | Core |
|---|---|---|
| Windows | ~/RetroFreakDumps | Not launched automatically |
| MiSTer | /media/fat/games/SNES | _Console/SNES, type=f, index=1, delay=2 |

- The save folder and the core can be changed with the SFC key in the settings file.

## CLI

- `identify-sfc`: checks the header (prints title, controller, chip, rom_size, readable, and limitation; does not save).
- `dump-sfc`: imports with confirmation. `--sfc-db` specifies a different DB.

## What verification means

- The bank reads of the five methods are compared with the reference traces. With synthetic data, the tests reconstruct the ROM with an independent address decoder and verify boundary banks, the order of the initialization packets, partial reception, disconnection, a different system, an unstable header, known chips and unsupported controls, DB fixing of capacities that are not powers of two, and recovery from and stopping on an invalid header.
- Verification of the dedicated methods: with synthetic data that differs per bank, the whole 8 MiB is reconstructed twice, and the full write order and the first-half/second-half boundary are checked. The tests also check CX4's initialization on each pass, that it can still be decided from the real header after the name changes to the DB's official name, and that an unpermitted title is refused.
- A header change, an incomplete reception, a removal, a shutdown failure, or cancellation just before the second read does not proceed to saving.
- The operation of the dedicated methods on a real adapter, and running a game that was read, have not been confirmed. A method that has not been confirmed is never displayed as confirmed.
- Game behavior is treated separately from the success of a read.
