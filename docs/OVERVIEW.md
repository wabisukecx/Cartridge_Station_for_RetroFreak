# Overview

[日本語](ja/OVERVIEW.md)

## Purpose

- Use the cartridge adapter that comes with the RetroFreak as a USB reader to import, verify, and save ROMs from cartridges the user owns.
- Use the imported ROMs in an emulator on a PC or phone, or on a core of the MiSTer FPGA.
- Six families are supported: FC/NES, GB/GBC, GBA, Mega Drive (MD), PC Engine (HuCard/SuperGrafx), and Super Famicom (SFC).

## Terms

- **Reference trace**: a record (addresses, sizes, and write order) obtained by running each bank-switching procedure on a synthetic bus. It is kept in `tests/fixtures` and serves as the baseline against which the read implementation is compared.
- **Candidate**: a title in the DB narrowed down from partial information about the cartridge (a header or partial hashes). It does not mean the real title has been confirmed.
- **Full-ROM hash**: the SHA-1 of the whole ROM (and the SHA-256 where recorded). It is the basis for confirming the title and revision and for deciding whether to save.
- **Two independent reads**: reading the whole ROM twice from the system-specific starting state, without reusing the first read's data, and confirming that the byte sequences match. For GBA and SFC, the mode setting and header check are also redone before the second read. This does not mean a power cycle or a hardware reset on every system. See CAPTURE_FLOW for the guarantees of each system.

## Design principles

- **Read-only.** Nothing is written to save data, flash, EEPROM, RTC, and so on. The only writes sent to a cartridge are a limited set of registers for ROM bank switching, ROM read setup, and RAM write protection.
- **Verify, then save.** Only data from two independent full-ROM reads that match each other and match the full-ROM hash in the bundled DB (SHA-1, and SHA-256 where recorded) is saved.
- **Never damage an existing file.** A save is published from a temporary file without replacing anything. A file with the same name and content is reused; a file with the same name and different content is left alone and the new ROM is saved under a hash-qualified name.
- **Do not proceed on guesses.** The board, capacity, and revision are not decided from a header or a title name alone. Anything unknown, mismatched, or unsupported stops with a reason.
- **Do not resend a failed transfer.** A process that hit a partial reception, an invalid header, or a send/receive exception does not continue. The session is closed and the user reconnects explicitly.
- **Do not bundle ROM data.** The bundled DB holds only metadata such as titles, sizes, and hashes.
- **Separate screens from processing.** Reading, identification, saving, and the generation of launch information belong to a shared service layer, which the screens (Windows GUI, MiSTer text menu) call. The CLI calls the per-system read code and the shared post-save verification directly.

## Out of scope

- Reading or writing save data, flash programming, and any write to a cartridge other than the limited registers above.
- CD-ROM systems; peripheral cartridges such as Sufami Turbo and the SF Memory cassette; SPC7110-specific operations; and CX4 other than one permitted title. ROM reading for SA-1 and S-DD1, and for the one permitted CX4 title, is implemented (tested on a synthetic bus; not verified on hardware).
- Adding anything other than commercially released cartridges (recent new releases, reissue-only releases, homebrew, hacks, translation patches, pirated copies, and unreleased prototypes) to the standard DB.
- Game library management. Listing and selecting saved games is handled by MiSTer's ConsoleMode.
- Building executables (exe) or obfuscating the source. The Python source and the standard DB are distributed as they are.
- Starting emulators automatically, and FTP transfer (Windows edition).

## Environment

| Item | Windows Edition | MiSTer FPGA Edition |
|---|---|---|
| Runtime | Python 3.9 or later (installed by the user) | The Python 3.9 and libusb that ship with MiSTer |
| Screen | PyQt6 GUI | English text menu operated with a TV and a gamepad |
| Communication | Serial (COM port), auto-detected or specified | Direct USB (bulk transfer through PyUSB) |
| Dependencies | pyserial, PyUSB, and PyQt6, installed with pip | Pure-Python sources of pyserial and PyUSB, bundled (vendor) |
| Default save location | `RetroFreakDumps` in the home folder | System-specific folders under `/media/fat/games` |
| Game launch | Not done | Starts the matching core with an MGL file and the command FIFO |

- The reader is USB VID:PID f00d:1337. It is the RetroFreak cartridge adapter and appears to the PC as a serial device (CDC ACM).
- A real NES (overseas Famicom) cartridge needs an NES cartridge converter to connect to the RetroFreak adapter. FC cartridges go straight into the FC slot. In processing, FC and NES are handled as one family.
- Only one process can use a reader at a time. Do not run the manager screen, the GUI, and the CLI at the same time.

## Layers

| Layer | Modules | Role |
|---|---|---|
| Entry point | cart_station.py | The only entry point; adds src to the search path and starts the CLI |
| CLI | cart_station_cli | Subcommands (list, probe, gui, manager, identify-\*/dump-\*, and so on) |
| Screens | cart_station_windows, cart_station_manager | The PyQt6 GUI and the MiSTer text menu. Both call only the service layer |
| Service | cart_station_service | Connection, insertion detection, candidate selection, import, verification, saving, and launch information |
| Presentation | cart_station_presentation | Columns and detail text for each system's candidates |
| Per-system reading | cart_station_nes_core / nes_mappers / nes_registers / nes_db, gb, gba, md, pce, sfc | Header recognition, bank traversal, ROM register checks, and DB matching |
| Shared DB | cart_station_rom_db | Full-ROM hash matching for the linear-ROM systems (GB/GBA/MD/PCE/SFC) |
| Storage | cart_station_storage | Publishing through a temporary file, and alternate names on conflict |
| Communication | cart_station_transport, cart_station_protocol | Serial/USB transport, packets, reply validation, and adapter operations |
| Settings and environment | cart_station_paths, environment, branding | Install location, settings-file resolution, PC/MiSTer detection, per-system save folders, and names |
| Measurement | cart_station_metrics | Records of import time and traffic |

- For reading, dependencies point as "screen → service → per-system reading → adapter → transport". The CLI calls the service's post-save verification and the per-system reading code; the service and the CLI use the same save code. The protocol layer does not import the UI or the CLI.
- The service has one worker that owns the transport, the candidates, and the file verification. A screen requests one operation at a time, from a dedicated thread (Windows) or a thread pool (MiSTer).

## Overall import procedure

1. **Connect**: open the transport and get the product information (build/product).
2. **Detect insertion**: read the cartridge state repeatedly, and treat it as stable when the same value appears three times in a row.
3. **Identify**: for each system, read the header or compute partial hashes to build candidates. The screen shows the title, board, capacity, and whether it is supported.
4. **Select**: if there is one candidate, it is selected automatically. Several candidates that can be read by the same procedure are decided by the full-ROM hash after reading. Otherwise the user chooses.
5. **Confirm**: reading starts only after the user confirms (the default is No). Merely inserting a cartridge does not read the whole ROM (PCE is the exception: it reads the whole ROM once on insertion to match the DB).
6. **Read**: run two independent reads.
7. **Verify**: check that the two reads match, the header, and the full-ROM hash in the DB.
8. **Save**: save only verified data, and re-check the saved file.
9. **Launch (MiSTer only)**: re-verify the saved ROM, then start the matching core with the ROM specified in an MGL file.

## What verification means

- A passing software test, a successful import of a real cartridge, and the game actually working are treated as separate facts. Being listed in the DB does not mean the read procedure has been implemented or confirmed.
- Whether a read is supported is decided by whether each DB profile fits the current read conditions (system, board, capacity, and submapper). A profile that does not fit has its import disabled, with the reason shown.
