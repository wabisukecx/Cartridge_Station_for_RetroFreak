# Cartridge Station for RetroFreak User Guide

[日本語の利用ガイド](README.ja.md) · [README](README.md) · [Design documents](docs/README.md)

A tool that connects the RetroFreak cartridge adapter over USB and imports ROMs from cartridges you own. One reading engine is used by two editions.

| Edition | Purpose | Launch |
|---|---|---|
| **Windows Edition** | Import ROMs for emulators on a PC or phone | `Cartridge_Station_for_RetroFreak_Windows.cmd` |
| **MiSTer FPGA Edition** | Import with just a TV and a gamepad, then play on an existing core | Scripts → `Cartridge_Station_for_RetroFreak` |

This tool is meant for backing up cartridges you own. It contains no ROM data and never reads or writes save data. It is an unofficial tool and is not affiliated with RetroFreak, CYBER Gadget, or the MiSTer project.

- Change history: [CHANGELOG.md](CHANGELOG.md)
- Index of the design documents: [docs/README.md](docs/README.md)

## Supported systems

| System | How the cartridge is identified | Details |
|---|---|---|
| FC (Famicom) | Search the DB for candidates by partial hash | [FC/NES](docs/SYSTEM_FC_NES.md) |
| GB/GBC | Header (MBC and capacity) | [GB/GBC](docs/SYSTEM_GB_GBC.md) |
| GBA | Header plus full-ROM hash | [GBA](docs/SYSTEM_GBA.md) |
| Mega Drive | Header | [MD](docs/SYSTEM_MD.md) |
| PC Engine (HuCard/SuperGrafx) | The data read and the DB | [PCE](docs/SYSTEM_PCE.md) |
| SFC (Super Famicom) | Header (LoROM/HiROM and special chips) | [SFC](docs/SYSTEM_SFC.md) |

Being listed in the DB is not the same as having confirmed a read of that cartridge. NES cartridges need an NES cartridge converter, and real NES hardware is unverified.

## What you need

- The cartridge adapter taken from the RetroFreak (it works on USB power; USB ID `F00D:1337`).
- **Windows**: Python 3.9 or later.
- **MiSTer**: the Python 3.9 and libusb that ship with MiSTer. pyserial and PyUSB are bundled in `vendor/` of the MiSTer ZIP, so you do not need to install them with pip.

## Installation

### Windows

Open PowerShell in the folder where you extracted the ZIP (or in the repository).

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
```

After that, start it by double-clicking `Cartridge_Station_for_RetroFreak_Windows.cmd`. For the design, see [Windows GUI](docs/FRONTEND_WINDOWS_GUI.md).

### MiSTer

Transfer `Cartridge_Station_for_RetroFreak_MiSTer.zip` to `/media/fat/Scripts` and extract it over SSH.

```sh
cd /media/fat/Scripts
unzip -o Cartridge_Station_for_RetroFreak_MiSTer.zip
```

Extracting over an existing install does not change your personal settings (`config/*.json`) or your saved ROMs. For the design of the layout and settings, see [Distribution](docs/DISTRIBUTION.md) and [Configuration](docs/CONFIGURATION.md). If you are moving from the old name (MiSTer Cart Station), see "Carrying over settings from the old name's install location" in the latter.

## How to use

### Windows

1. Choose the connection method (Auto-detect or a COM port) and the save folder, then press **Connect**. Merely starting the tool does not connect to the reader.
2. When you insert a cartridge, the tool checks it automatically and shows candidates. If there are several candidates and it cannot decide automatically, choose the title you inserted from the list.
3. Press **Read cartridge**, check the title and the save folder, and choose **Yes**.
4. When the two reads match and the result also matches the full-ROM hash in the DB, the ROM is saved.
5. Use **Open save folder** to open the saved ROM.

For PCE, the automatic identification after connecting reads the whole ROM once. It does not save at that stage; after you confirm **Read cartridge**, it does two independent reads and saves. The procedure on MiSTer is the same.

During a read or identification, **Cancel reading** stops it at a safe boundary.

### MiSTer (TV and gamepad)

The menu is arranged from the top in the order you use it.

| Menu | Meaning |
|---|---|
| CHECK / RETRY | Check the connection and the cartridge again. Also where you resume after an error |
| SELECT CARTRIDGE | Choose the title you inserted when there are several candidates |
| DUMP CARTRIDGE | Choose YES on the confirmation screen (the default is NO) to start the import |
| GAME START | Re-verify the saved ROM and start it on the matching core |
| EXIT TO MiSTer | Return to the MiSTer menu |

Choose with the D-pad, run with the OK button, and cancel with the Back button. From a game, Reboot returns you to ConsoleMode. To make a newly imported game appear in ConsoleMode's list, rebuild the database on the ConsoleMode side. For the design, see [MiSTer manager](docs/FRONTEND_MISTER_MANAGER.md).

## How saving works

- **Conditions for saving**: a ROM is saved only when two independent reads match and the result also matches the full-ROM hash in the bundled DB (SHA-1, and SHA-256 where recorded).
- **File name**: the title name in the DB is used.
- **When a file with the same name exists**: if its content is the same, it is reused; if it differs, the original file is kept and the new ROM is saved under a hash-qualified name. For the design, see [Import, verification, and saving](docs/CAPTURE_FLOW.md).
- **How it is written**: it is written to a temporary file and then published in a way that does not replace an existing file. An existing ROM with the same name and different content is kept.

## Settings

The settings for the save folders and the cores are in `config/`. If you use the defaults, no settings are needed.

| File | Meaning | Default |
|---|---|---|
| `output_paths.json` | The save folder for each system | PC: `~/RetroFreakDumps`; MiSTer: `/media/fat/games/<core's folder>` |
| `mister_cores.json` | The core that GAME START opens | MiSTer's standard cores, such as `_Console/NES` |

The ZIP and the repository contain only `output_paths.example.json` and `mister_cores.example.json`, which hold the default values. **To change a setting, copy the `.example.json` file, remove `.example` from the name, and edit the copy.**

```sh
cd config
cp output_paths.example.json output_paths.json    # on Windows, use copy
```

- **Order of reading**: if a `.json` file exists it is used; otherwise the `.example.json` file is used.
- **Not erased by updates**: extracting a newer ZIP over the old one does not overwrite a `.json` file you created. If you edit a `.example.json` file directly, it returns to the defaults at every update.
- **Not uploaded to Git**: `.json` files are covered by `.gitignore` and are not in the repository.

The command line's `--output-dir` has the highest priority for the save folder. To put the settings file somewhere else, specify it with `--paths-config`.

## When something does not work

- **Check the reader's state**: `python cart_station.py --port COM5 probe` (on MiSTer, `--usb probe`) shows the product information and the cartridge state.
- **Check the MiSTer environment**: `sh cart_station_check.sh` shows the state of Python, the communication libraries, and USB device recognition (it sends no command to the reader).
- **Communication errors**: the screen shows details such as the number of bytes received. For the design, see [Communication design](docs/PROTOCOL.md).
- **Import time**: the most recent import is recorded in `runtime/last_capture_metrics.json`. What is recorded is described in [Import, verification, and saving](docs/CAPTURE_FLOW.md).
- **MiSTer log**: `runtime/manager.log`.

## Command line

```sh
python cart_station.py --help
python cart_station.py list                    # list serial ports (does not connect)
python cart_station.py --usb identify-sfc      # check the header
python cart_station.py --usb dump-sfc          # import
python cart_station.py list-db --query "<part of a title>" # search the FC DB (does not connect)
```

Each system has `identify-*` and `dump-*` commands (`sfc`, `md`, `gba`, `gb`, `pce`, and `db` for FC). For the list of commands, see [Command-line design](docs/CLI.md).

## License

This project is under GPL-3.0-only ([LICENSE](LICENSE)). The full license texts of pyserial and PyUSB are in `licenses/`. For how dependencies are handled, including PyQt6 and the Qt-related packages that the user installs with pip, see [LICENSES.md](docs/LICENSES.md).
