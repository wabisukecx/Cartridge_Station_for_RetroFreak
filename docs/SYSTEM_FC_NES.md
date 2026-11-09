# FC/NES read design

[日本語](ja/SYSTEM_FC_NES.md)

## Scope and assumptions

- FC (Famicom) cartridges are read in the FC slot. A real NES (overseas) cartridge is connected through an NES cartridge converter. Processing does not distinguish them.
- The cartridge state code is 3.
- Only PRG ROM and CHR ROM are read. It does not read or write CHR RAM, PRG RAM, saves (battery backup), EEPROM, flash, sound chips, or expansion memory.
- The output is a .nes file: the DB's header (16-byte iNES/NES 2.0) followed by the PRG and CHR. The header is never guessed; the verified DB header is used.
- The supported range is decided by "whether each DB profile fits the read conditions below". A profile outside the conditions has its import disabled, with the reason shown.

## Identification

- With no writes and no bank switching, it looks for DB candidates using partial hashes of the first 1 KiB and last 1 KiB of the initial PRG (a 1 KiB dummy read from 0x8000, then 1 KiB each from 0x8000 and 0xFC00).
- If there is one candidate, it is selected. Several candidates with the same read configuration are decided by the full-ROM hash after reading (automatic decision). Otherwise the user chooses.
- It identifies once more right before the import starts, and stops if the selected candidate is not included.

## Basic structure of reading

`MapperReader` holds the bank traversal and the selection procedures, and operates registers through `adapter.nes` (`cart_station_nes_registers.NesRegisters`). The checks of allowed addresses and values, and the sequences of writes that include protection, are gathered in this dedicated module, so the transport layer has no branching on mapper numbers. The shared bank traversal refers directly to `cart_station_banks.read_banks`.

1. Start the mode (04(1), 05, a 200 ms wait).
2. For each bank, write the mapper's registers, then read from CPU space (command 0x11) or PPU space (command 0x13). PRG is in units of 8/16/32 KiB and CHR in units of 1–8 KiB and so on, following each mapper's window size.
3. Read all the banks in order, and concatenate PRG then CHR.
4. Do the same procedure once more (two independent reads) and confirm the two match.
5. Match the SHA-1 of the PRG, the CHR, PRG+CHR, and (if present) the finished file against the DB.
6. End the mode (04(0), 05). It is attempted whether the read succeeds, fails, or is cancelled.
- The bank traversal belongs to a shared function. Bank numbers, remainders, progress, and detection of short replies are shared, and only the bus operations are swapped per mapper.
- If a communication failure occurs, it does not continue later register operations and does not retry. It does not guarantee that the closing of an outer gate or the like has completed. The normal mode shutdown still runs.

### Policy for register writes

- Only ROM bank-switching registers are written. After a normal write (command 0x12), it does a dummy read of 8 bytes from 0x8000.
- Besides the generic write entry point (0x8000–0xFFFF, values 0–255), there are board-specific entry points. A dedicated entry point strictly limits the addresses and values allowed, and never reaches enabling save RAM, ID or unlock keys, flash programming, or EEPROM operations. Boards that need registers at low addresses (0x4000–0x7FFF) use a dedicated entry point.
- For boards with bus conflicts, it looks for an address in the ROM that holds the same value as the byte to be written, and writes there (a search-write).

### Preserving the order of communication

- Using the reference traces as the baseline, the read addresses, sizes, and write order are compared. Corrections specific to a board (added initialization, SRAM-protection writes, operation of an outer bank gate) are compared as differences inserted into the reference trace and stated explicitly.

## Supported mappers

### Mapper 0 (NROM)

- Submapper 0, PRG 16/32 KiB, CHR 0/8 KiB. No bank switching. PRG is read 16 KiB at a time from 0x8000, and CHR is 8 KiB from PPU 0.

### Overall conditions

- Only the submappers shown in the table are supported. A submapper not shown is unsupported.
- The capacity must be within the table's range and a multiple of the unit. It is limited to sizes that the DB's header can express.
- The conditions in the table are "conditions under which a read can be attempted", not a guarantee of success for every title and board. A board not verified on hardware says so explicitly.

### Table

| Mapper | Reference trace | Conditions |
|---|---|---|
| 1 | mapper001, mapper001_chr8k, mapper001_prg64k | Submapper 0, PRG 32–512 KiB, CHR 0–128 KiB. The variants for CHR 8 KiB and for PRG 64 KiB that fit the size are also matched by the full-ROM hash |
| 1.5 | mapper001 with a path added for fixed PRG | Fixed PRG 32 KiB, CHR ROM 8/16/32/64/128 KiB. No PRG bank operations |
| 2 | mapper002 | Submapper 0/1/2, PRG in units of 16 KiB up to 4 MiB, no CHR ROM |
| 3 | mapper003, mapper003_ex | Submapper 0/1/2, PRG 16/32 KiB, CHR in units of 8 KiB up to 2 MiB. Above 32 KiB of CHR it uses the bank values of the Ex variant |
| 4 | mapper004 | Submapper 0/1/3, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 1 KiB up to 256 KiB |
| 5 | mapper005 (MMC5), with the initial mode corrected | Submapper 0, PRG in units of 8 KiB from 32 KiB to 1 MiB, CHR in units of 2 KiB from 8 to 512 KiB. SRAM protection added. The upper-bit path for large CHR is unsupported |
| 7 | mapper007 (AxROM) | Submapper 0/1/2, PRG in units of 32 KiB from 32 KiB to 8 MiB, no CHR ROM. Search-write switching by byte value |
| 9 | mapper009 (MMC2) | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 4 KiB from 8 KiB to 1 MiB. The upper limit is the range a bank number (a byte value) can express |
| 10 | mapper010 (MMC4) | Submapper 0, PRG in units of 16 KiB from 32 KiB to 4 MiB, CHR under the same conditions as 9. ROM only; saves are not read or written |
| 11 | mapper011 (Color Dreams) | Submapper 0, PRG in units of 32 KiB up to 128 KiB, CHR in units of 8 KiB up to 128 KiB. Not verified on hardware |
| 13 | mapper013 (CPROM) | Submapper 0, fixed PRG 32 KiB, no CHR ROM only. CHR RAM is not read. Not verified on hardware |
| 16 | mapper016 (Bandai FCG) | Submapper 4/5, PRG in units of 16 KiB from 16 to 256 KiB, CHR in units of 1 KiB from 8 to 256 KiB. Submapper 0 and others are unsupported |
| 18 | mapper018 (Jaleco SS88006) | Submapper 0, PRG in units of 8 KiB from 32 to 512 KiB, CHR in units of 1 KiB from 8 to 256 KiB. Sets the split registers |
| 19 | mapper019 (Namco 129/163) | Submapper 0/2/3/4/5, PRG and CHR capacities as for 18. Reads PRG from CPU 0xA000, and keeps the DB's submapper in the header |
| 21 | mapper021_vrc4a / mapper021_vrc4c | Submapper 1/2, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 1 KiB from 8 to 512 KiB. Submapper 0 is unsupported |
| 22 | mapper022 (VRC2a) | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 1 KiB from 8 to 256 KiB. The PRG window is CPU 0xA000, and the CHR number is shifted left by 1 bit and set in split registers |
| 23 | mapper023_vrc4f / mapper023_vrc4e / mapper023_vrc2b | Submapper 1/2/3. PRG as for 21; CHR up to 512 KiB for 1 and 2, and up to 256 KiB for 3. Submapper 0 is unsupported |
| 25 | mapper025_vrc4b / mapper025_vrc4d / mapper025_vrc2c | Submapper 1/2/3. Capacity conditions as for 23. Submapper 0 is unsupported |
| 24 / 26 | mapper024 / mapper026 (VRC6a/b) | Submapper 0, PRG in units of 16 KiB from 32 KiB to 4 MiB, CHR in units of 1 KiB from 8 to 256 KiB. Sets 0xB003 to 0 for each CHR bank |
| 30 | mapper030 (UNROM512) | Submapper 0, PRG in units of 16 KiB from 16 to 512 KiB, no CHR ROM. Search-write over 0xC000–0xFFFF. Flash rewriting is not done |
| 32 | mapper032 (Irem G-101) | Submapper 0: PRG in units of 8 KiB from 32 KiB to 2 MiB. Submapper 1: 32–256 KiB, fixed 8+8+16F layout. CHR for both: units of 1 KiB from 8 to 256 KiB |
| 33 | mapper033 (Taito TC0190) | Submapper 0, PRG and CHR capacities as for 32. The CHR read window is PPU 0x1000 |
| 34 BNROM | mapper034_bnrom | Submapper 0/2, PRG in units of 32 KiB from 32 KiB to 8 MiB, no CHR ROM |
| 34.1 | mapper034_nina001 | PRG 32/64 KiB, CHR in units of 4 KiB from 8 to 64 KiB. Dedicated writes to 0x7FFD/0x7FFE only. Not verified on hardware |
| 37 / 47 | mapper037 / mapper047, with an outer gate added and the 47 boundary corrected | Submapper 0, PRG 256 KiB + CHR 256 KiB, no battery and no PRG RAM specified. Read with PRG at 8 KiB and CHR at 1 KiB. Not verified on hardware |
| 48 | mapper048 (Taito TC0690) | Submapper 0/1, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 1 KiB from 8 to 256 KiB. The CHR window is PPU 0x1000 |
| 64 | mapper064 (Tengen RAMBO-1) | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 1 KiB from 8 to 256 KiB. The CHR selector is 32, and the PPU window is 0 |
| 65 | mapper065 (Irem H3001) | Submapper 0, PRG and CHR capacities as for 32. The PRG read window is CPU 0xA000 |
| 66 | mapper066 (GxROM) | Submapper 0, PRG in units of 32 KiB up to 512 KiB, CHR in units of 8 KiB up to 128 KiB. Limited to what a byte value and a 4-bit mask can express |
| 67 | mapper067 (Sunsoft-3) | Submapper 0, PRG in units of 16 KiB up to 4 MiB, CHR in units of 2 KiB from 8 to 512 KiB |
| 68.0 | mapper068 (Sunsoft-4, normal) | PRG in units of 16 KiB up to 256 KiB, CHR in units of 2 KiB from 8 to 512 KiB |
| 68.1 | mapper068_ex (Sunsoft-4, "Nante + Kogame") | PRG 128/144 KiB, CHR in units of 2 KiB from 8 to 512 KiB. Up to 8 internal banks plus 1 external bank |
| 69 | mapper069 (Sunsoft FME-7) | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 1 KiB from 8 to 256 KiB. Commands 9/0 select PRG/CHR |
| 70 | mapper070 | Submapper 0, PRG in units of 16 KiB up to 256 KiB, CHR in units of 8 KiB up to 128 KiB |
| 71 | mapper071 | Submapper 0, PRG in units of 16 KiB up to 4 MiB, no CHR ROM, not verified on hardware |
| 72 | mapper072 | Submapper 0, PRG in units of 16 KiB from 16 KiB to 2 MiB, CHR in units of 8 KiB from 8 to 512 KiB. Reads from CPU 0x8000 after a two-step search-write |
| 73 | mapper073 (VRC3) | Submapper 0, PRG in units of 16 KiB from 16 KiB to 4 MiB, no CHR ROM. 0xF000 switches PRG |
| 75 | mapper075 (VRC1) | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 4 KiB from 8 to 128 KiB |
| 76 | mapper076 (NAMCOT-3446) | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 2 KiB from 8 to 512 KiB |
| 77 | mapper077 | Submapper 0, PRG in units of 32 KiB from 32 to 512 KiB, CHR in units of 2 KiB from 8 to 32 KiB. Reads only the 2 KiB ROM window at PPU 0 |
| 78 | mapper078 | Submapper 0/1/3, PRG in units of 16 KiB up to 128 KiB, CHR in units of 8 KiB up to 128 KiB. The DB submapper is kept in the header |
| 80 | mapper080 (Taito X1-005) | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 1 KiB from 8 to 256 KiB. The low registers use write command 0x15 |
| 82 | mapper082 (Taito X1-017) | Submapper 0, PRG in units of 8 KiB from 32 to 512 KiB, CHR in units of 1 KiB from 8 to 256 KiB. Does initialization, a 2-bit left shift of the PRG number, and CHR pair setup |
| 552 | mapper552 (Taito X1-017, PRG register wired with the 6 bits reversed) | Submapper 0, PRG in units of 8 KiB from 32 to 512 KiB, CHR in units of 1 KiB from 8 to 256 KiB. Initialization as for 82. The PRG number is written to 0x7EFA with its 6 bits reversed, and CHR only to 0x7EF2 (0x7EF3 is not written) |
| 85 | mapper085 (VRC7) | Submapper 0/1/2, PRG in units of 8 KiB from 32 KiB to 2 MiB. No CHR ROM, or CHR in units of 1 KiB from 8 to 256 KiB |
| 86 | mapper086 (JF-13) | Submapper 0, PRG in units of 32 KiB from 32 to 128 KiB, CHR in units of 8 KiB from 8 to 64 KiB. Dedicated write to 0x6000 |
| 87 | mapper087 | Submapper 0, fixed PRG 16/32 KiB, CHR 16/32 KiB. In each read, limited CHR switching is done only after the whole PRG matches |
| 88 | mapper088 | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 2 KiB from 8 to 128 KiB. From 64 KiB it switches to the upper window and pair setup |
| 89 | mapper089 (Sunsoft-2) | Submapper 0, PRG in units of 16 KiB up to 128 KiB, CHR in units of 8 KiB up to 128 KiB. CHR number bit 3 is placed in register bit 7 |
| 92 | mapper092 | Submapper 0, capacity conditions as for 72. The PRG window is CPU 0xC000 |
| 93 | mapper093 | Submapper 0, PRG in units of 16 KiB from 16 to 128 KiB, no CHR ROM. After a write to 0x6000, it searches 0xD000–0xFFFF and writes |
| 94 | mapper094 (HVC-UN1ROM) | Submapper 0, PRG in units of 16 KiB up to 1 MiB, no CHR ROM |
| 95 | mapper095 (NAMCOT-3425) | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 1 KiB from 8 to 256 KiB. Reads from PPU 0x1000 |
| 96 | mapper096 | Submapper 0, PRG in units of 32 KiB from 32 to 128 KiB, no CHR ROM only. CHR RAM is not added to the ROM |
| 97 | mapper097 (Irem TAM-S1) | Submapper 0, PRG in units of 16 KiB up to 4 MiB, no CHR ROM. Writes to 0x8000 and reads from 0xC000 |
| 118 | mapper118 | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 1 KiB from 8 to 128 KiB |
| 119 | mapper119 | Submapper 0, PRG as for 118, CHR ROM in units of 1 KiB from 8 to 64 KiB. Not verified on hardware; CHR RAM is not a read target |
| 140 | mapper140 | Submapper 0, PRG in units of 32 KiB from 32 to 512 KiB, CHR in units of 8 KiB from 8 to 128 KiB. Dedicated write to 0x6000 |
| 148 | mapper148 | Submapper 0, PRG 32/64 KiB, CHR in units of 8 KiB from 8 to 64 KiB. Not verified on hardware |
| 152 | mapper152 | Submapper 0, PRG in units of 16 KiB up to 128 KiB, CHR in units of 8 KiB up to 128 KiB |
| 153 | mapper153 (Bandai FCG + SRAM) | Submapper 0, PRG in units of 16 KiB from 32 to 512 KiB, no CHR ROM. SRAM is disabled before each bank. Reading and writing saves is out of scope |
| 154 | mapper154 (NAMCOT-3453) | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 2 KiB from 8 to 128 KiB. Switches the pair setup and the PPU window at the 64 KiB boundary |
| 155 | mapper155 (MMC1A) | Submapper 0, PRG in units of 16 KiB from 32 to 512 KiB, CHR in units of 4 KiB from 0 to 128 KiB. Title-specific MMC1 variants are not tried |
| 157 | mapper157 (Datach Joint ROM) | Submapper 0, PRG in units of 16 KiB from 16 KiB to 4 MiB, no CHR ROM. Sets four outer-number registers and the PRG number |
| 158 | mapper158 | Submapper 0, PRG as for 64, CHR in units of 1 KiB from 8 to 128 KiB. Not verified on hardware |
| 159 | mapper159 (Bandai FCG) | Submapper 0, capacity conditions as for 16. EEPROM operations are out of scope |
| 180 | mapper180 | Submapper 0, PRG in units of 16 KiB up to 4 MiB, no CHR ROM. The read window for the switchable PRG is 0xC000 |
| 184 | mapper184 (Sunsoft-1) | Submapper 0, fixed PRG 16/32 KiB, CHR in units of 4 KiB from 8 to 32 KiB. CHR is selected only after the fixed PRG is verified |
| 185 | mapper185 (CNROM-CP) | Submapper 4/5/6/7, fixed PRG 16/32 KiB. Only the unlock of a known PRG CRC is allowed. Normal CHR is 8 KiB; only 9CF17FAB has 16 KiB. 49FF2E0B is limited to submapper 5, PRG 32 KiB, and CHR 8 KiB. The unlock happens after the SHA-1 and the actual CRC match |
| 186 | mapper186 (StudyBox) | Submapper 0, PRG in units of 16 KiB from 16 KiB to 4 MiB, no CHR ROM. The first bank is read from 0xC000; after that a number is selected with 0x4201 and read from 0x8000 |
| 188 | mapper188 / mapper188_ex | Submapper 0, PRG in units of 16 KiB from 16 to 256 KiB, no CHR ROM. The internal/external selection value changes at the boundary of bank 8 |
| 206.0 | mapper206 | PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 1 KiB from 8 to 256 KiB. Bank-switching type |
| 206.1 | mapper206_prg32k | Fixed PRG 32 KiB, CHR in units of 1 KiB from 8 to 256 KiB. It does not write to the PRG registers and reads from two 16 KiB windows |
| 207 | mapper207 | Submapper 0, PRG and CHR capacities as for 80. The low registers use the normal write command 0x12 |
| 210 | mapper210 (Namco 175/340) | Submapper 0/1/2, PRG in units of 8 KiB from 32 to 512 KiB, CHR in units of 1 KiB from 8 to 256 KiB. The PRG number is limited to 6 bits |
| 406 | mapper406_axr / mapper406_inl | Submapper 0, PRG in units of 8 KiB from 32 KiB to 2 MiB, CHR in units of 1 KiB from 8 to 256 KiB. The ROM register variant is chosen by the full-ROM hash. INL is not verified on hardware |

### Mapper 185 CHR unlock values

| PRG CRC32 | Order of unlock values |
|---|---|
| 2A629F7D | 240 → 15 |
| 725D5CC4 | 0 → 51 |
| AA755715 | 0 → 17 |
| 87A3F91E / DAADEBFA / 81A743F5 | 32 → 34 |
| 73DE64AD | 0 → 42 |
| B36457C7 | 19 → 33 |
| A03C98A7 | 33 → 32 |
| 9CF17FAB | For CHR bank 0, 16 → 18; next, 0 → 19 |
| 49FF2E0B | 0 → 17 (submapper 5, PRG 32 KiB, CHR 8 KiB) |

### VRC2/VRC4 upper CHR registers

| Mapper.Submapper | Implementation | Upper CHR register |
|---|---|---|
| 21.1 | mapper021_vrc4a | 0xB002 |
| 21.2 | mapper021_vrc4c | 0xB040 |
| 23.1 | mapper023_vrc4f | 0xB001 |
| 23.2 | mapper023_vrc4e | 0xB004 |
| 23.3 | mapper023_vrc2b | 0xB001 |
| 25.1 | mapper025_vrc4b | 0xB002 |
| 25.2 | mapper025_vrc4d | 0xB008 |
| 25.3 | mapper025_vrc2c | 0xB002 |

### Design notes per board

#### Mapper 1 (MMC1) family
- Normal (submapper 0) is PRG 32–512 KiB and CHR 0–128 KiB. A board whose CHR is fixed at 8 KiB and a board whose PRG is 64 KiB have separate read variants, and when several variants fit, the variant is chosen by the full-ROM hash of the first read. If the second read fails for a variant that matched once, it stops without retrying.
- 1.5 (submapper 5) is a board whose PRG is physically fixed at 32 KiB (SEROM/SHROM/SH1ROM). It does no PRG bank operations and reads only the CHR ROM banks.

#### Mapper 5 (MMC5)
- Submapper 0, PRG in units of 8 KiB from 32 KiB to 1 MiB, CHR in units of 2 KiB from 8 to 512 KiB.
- The PRG mode (0x5100) is set to PRG mode 3, to be consistent with selecting 8 KiB windows.
- At the start of every read initialization, it sets the SRAM write protection (0 to 0x5102 and 0x5103). It does not use the save-unlock values or the registers for RAM selection, extended RAM, or the sound chip.
- For PRG it writes the number with the ROM-select bit set to 0x5114, and reads 8 KiB from 0x8000.
- The upper-bit path for large CHR is unsupported, so CHR is limited to configurations that do not exceed the limit.

#### Mapper 37/47 (multi-game boards)
- Limited to submapper 0, PRG 256 KiB + CHR 256 KiB, with no battery and no PRG RAM specified.
- It selects the outer bank at 0x6000, and with the MMC3 selector 6 reads PRG 8 KiB from 0x8000, and with selector 2 reads CHR 1 KiB from PPU 0x1000.
- For 37, the first 64 KiB of PRG is selected with outer bank 8, the next 64 KiB with 11, and the remaining 128 KiB with 12. For CHR, the first 128 KiB is 8 and the second half is 12.
- For 47, the outer bank is handled in units of 16 PRG banks of 8 KiB and 128 CHR banks of 1 KiB.
- Only when it updates the outer latch does it open the write gate (0xA001=128), write to 0x6000, and then close it.

#### Mapper 32 submapper 1
- PRG is fixed as 8+8+16F and the mode-switch bit is not used. It specifies mode 0 with 0x9000=0, writes the bank number to 0x8000, and reads from 0x8000. CHR uses 0xB000. The PRG limit is 256 KiB.

#### Mapper 82/552 (Taito X1-017)
- 82 does initialization (255 to 0x7EF7/8/9 and 0 to 0x7EF6), writes the PRG number to 0x7EFA (shifted left by 2 bits), and sets up the CHR pair.
- 552 is a board where the 6 bits of the PRG register are wired in reverse order. It writes the PRG number with its 6 bits reversed to 0x7EFA, and the CHR number to 0x7EF2. Initialization is the same as for 82, and 0x7EF3, used for the CHR pair, is not written.
- 82 and 552 have a 6-bit PRG register, so PRG is at most 512 KiB.

#### Mapper 34 (BNROM / NINA-001)
- BNROM (submapper 0/2): PRG in units of 32 KiB, with no CHR ROM.
- NINA-001 (submapper 1): uses a dedicated entry point that writes the PRG number (0/1) to 0x7FFD and the CHR number (0–15) to 0x7FFE. The only addresses allowed are 0x7FFD and 0x7FFE. PRG 32/64 KiB, CHR in units of 4 KiB from 8 to 64 KiB.

#### Mapper 153 (Bandai FCG + SRAM)
- Before reading each PRG bank, it writes the SRAM disable (0x800D=0). It writes the number ÷ 16 to 0x8000–0x8003 and the number to 0x6008 and 0x8008. Bank numbers are 0–31. It does not read or write saves.

#### Mapper 186 (StudyBox)
- The first PRG bank is read 16 KiB from 0xC000 with no writes. After that it writes the number (1–255) to the fixed I/O port 0x4201 and reads from 0x8000. CHR RAM is not included in the output.

#### Mapper 406
- Two variants, AXR (selector 0x8000 / data 0x8002) and INL (selector 0xE000 / data 0xE001), are tried in the order AXR, INL, and decided by the full-ROM hash.

#### Mapper 185 (CNROM-CP)
- PRG is fixed. The CHR ROM is protected, and only the unlock writes (a pair of two values) for a known PRG CRC32 are allowed. An unregistered CRC stops before any communication.
- Normally CHR is 8 KiB. Only CRC 9CF17FAB has 16 KiB. CRC 49FF2E0B (Mighty Bomb Jack PRG1) sends 0 → 0x11 only for submapper 5, PRG 32 KiB, and CHR 8 KiB.
- It releases the CHR protection only after confirming with the full SHA-1 of the fixed PRG and the actual CRC.

#### Mapper 87
- PRG is fixed at 16/32 KiB. Only after the whole PRG matches in each read does it switch CHR through the dedicated entry point at 0x6000–0x7FFF (values 0–3).

#### Mapper 206 submapper 1
- PRG is fixed at 32 KiB. It does not write to the PRG registers and reads from two 16 KiB windows. CHR is in units of 1 KiB.
- Submapper 0 (the normal 206) is the bank-switching type.

#### Limits on capacity
- In some places bank numbers are handled as byte values (0–255), and the upper limits in the table are limited to the range that can be expressed. A capacity beyond the limit stops before any communication.
- Boards with only CHR RAM, and title-specific variants of MMC1, are out of scope.

## Measurement and optimization

- Communication dominates the import time. A fixed search area (the search-writes of Mappers 2 and 3) is reused only within one read, and the second read searches again and re-reads the body, to preserve the independence of the two independent reads.

## What verification means

- Each mapper is verified by reconstructing the ROM with a model that independently imitates the address decoding. A board not verified on hardware, or one for which there is no real cartridge, is verified only by the model.
- Being listed in the DB, having the read implemented, and having succeeded on a real cartridge are treated as separate facts.
