# Cartridge Station for RetroFreak

[User guide](USER_GUIDE.md) · [日本語版 README](README.ja.md) · [日本語のドキュメント](docs/ja/README.md)

A Python tool that uses the RetroFreak cartridge adapter as a USB reader to import, verify, and save ROMs from cartridges you own. One reading engine is shared by two editions.

| Edition | Purpose | Interface |
|---|---|---|
| **Windows Edition** | Import ROMs for emulators on a PC or phone | PyQt6 GUI |
| **MiSTer FPGA Edition** | Import on a DE10-Nano/MiSTer by itself, then start the game on an existing core | Text menu operated with a TV and a gamepad |

> [!IMPORTANT]
> This tool is meant for backing up **cartridges you own**. No ROM data is bundled or distributed. It is an unofficial tool and is not affiliated with RetroFreak, CYBER Gadget, or the MiSTer project.

## Features

- **Six systems**: FC/NES, GB/GBC, GBA, Mega Drive, PC Engine (HuCard/SuperGrafx), and Super Famicom.
- **Automatic identification**: detects the inserted cartridge and finds candidates from the header or partial hashes (PC Engine uses a full-ROM hash). You choose a candidate only when the tool cannot decide.
- **Verify before saving**: a ROM is saved only when two independent reads match and the result also matches the full-ROM hash (SHA-1, and SHA-256 where available) in the bundled database.
- **Existing files are never damaged**: files are published atomically from a temporary file. An identical file is reused; a different file with the same name is kept and the new ROM is saved under a hash-qualified name.
- **ROM reads only**: nothing is written to save data or flash.
- **FC/NES mappers**: 71 mapper numbers in the bundled database, including 0–5, the MMC and VRC families, Namco, Sunsoft, Taito, Bandai FCG, 206, and 552.
- **GB/GBC controllers**: ROM-read control for MBC1/2/3/5/6/7, HuC1/HuC3, TAMA5, and Pocket Camera.

> [!NOTE]
> Operation on real hardware has been confirmed only for some systems and methods (for example, NES and the SFC SA-1/S-DD1/CX4 chips are unconfirmed). The confirmed scope is recorded separately in [Hardware validation](docs/HARDWARE_VALIDATION.md).

See the [design documents](docs/README.md) for details. Being listed in the database is not the same as having been read successfully on real hardware.

## Requirements

- The cartridge adapter that comes with the RetroFreak (USB VID:PID `f00d:1337`).
- NES cartridges need an NES cartridge converter (real NES hardware is unverified).
- **Windows**: Python 3.9 or later.
- **MiSTer**: the Python 3.9 and libusb that ship with MiSTer (pyserial and PyUSB are bundled in the MiSTer ZIP).

## Installation and launch

### Windows Edition

If you downloaded the Windows ZIP, extract it and start from `py -m venv .venv` (skip the `git clone` and `cd` lines).

```powershell
git clone <this repository's URL>
cd Cartridge_Station_for_RetroFreak
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\Cartridge_Station_for_RetroFreak_Windows.cmd
```

On startup the tool only lists the ports. It does not connect to the reader until you press **Connect**. See [Windows GUI](docs/FRONTEND_WINDOWS_GUI.md) for the design.

### MiSTer FPGA Edition

If you downloaded the MiSTer ZIP, go straight to "Install it on MiSTer" below.

Running on MiSTer requires the distribution ZIP (MiSTer does not have pyserial or PyUSB, so the ZIP bundles them in `vendor/`). Copying the repository files as they are will not work.

**Build the ZIP** (once, on a PC; the `dist/` folder is created automatically):

```powershell
git clone <this repository's URL>
cd Cartridge_Station_for_RetroFreak
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe tools\build_mister_bundle.py
```

This produces `dist\Cartridge_Station_for_RetroFreak_MiSTer.zip`. If the same ZIP is available under Releases, you can use it instead of building it.

**Install it on MiSTer**: transfer the ZIP to `/media/fat/Scripts` (SMB share, SCP, copying to the SD card directly, and so on), then extract it over SSH:

```sh
cd /media/fat/Scripts
unzip -o Cartridge_Station_for_RetroFreak_MiSTer.zip
```

Start **Cartridge_Station_for_RetroFreak** from the MiSTer Scripts menu. From a terminal, run `python3 cart_station.py --usb manager`. See [MiSTer manager](docs/FRONTEND_MISTER_MANAGER.md) and [Distribution](docs/DISTRIBUTION.md) for the design.

### How an import works

1. Insert a cartridge. It is identified automatically.
2. Choose a candidate only if there are several.
3. Confirm the start, and the tool runs two independent reads for saving. (PC Engine also reads the whole ROM once during identification, but does not save it.)
4. When the two reads and the full-ROM hash all match, the ROM is saved.

## Configuration

The save folders (`config/output_paths.json`) and the cores that MiSTer opens (`config/mister_cores.json`) need no configuration if you keep the defaults. To change them, copy the `*.example.json` file in the same folder, remove `.example` from the name, and edit the copy. If a `.json` file exists it takes priority; it is not erased when you extract a newer ZIP over the old one, and it is not included in Git. For details, see [Configuration](docs/CONFIGURATION.md).

## Command line

```sh
python cart_station.py --help
python cart_station.py list                 # list serial ports (does not connect)
python cart_station.py --port COM5 probe    # product information and cartridge state
python cart_station.py --usb identify-gb    # check the header
python cart_station.py --usb dump-gb        # import
python cart_station.py list-db --query "<title keyword>"  # search the NES DB (does not connect)
```

Each system has `identify-*` and `dump-*` commands (`sfc`, `md`, `gba`, `gb`, `pce`, and `db` for FC/NES).

## Layout

```
cart_station.py           entry point (the implementation is in src/)
src/
  cart_station_protocol.py    packet format and shared memory operations
  cart_station_transport.py   serial / USB bulk transfer and adapter control
  cart_station_service.py     import service (identify, verify, save, start a MiSTer core)
  cart_station_storage.py     atomic save that never overwrites
  cart_station_nes_db.py      NES metadata and partial-hash identification
  cart_station_nes_mappers.py per-mapper FC/NES reads
  cart_station_{gb,gba,md,pce,sfc}.py  per-system reads
  cart_station_windows.py     Windows GUI (PyQt6)
  cart_station_manager.py     MiSTer manager screen
data/                     bundled DBs (title, size and hash metadata only; no ROM data)
config/                   configuration examples (*.example.json); personal settings are outside Git
tests/                    shared tests (use a simulated reader; no hardware needed)
windows_tests/            Windows GUI tests (need PyQt6)
tools/                    development scripts (distribution ZIP builders, hardware diagnostics, and so on)
docs/                     design documents (overview, protocol, DB, distribution, systems, screens)
docs/ja/                  the same documents in Japanese (originals)
```

The normal ZIPs do not include `tools/`, `tests/`, or `windows_tests/`; the validation ZIPs do.

## Tests

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests          # shared tests

python -m pip install -r requirements-windows.txt
python -m unittest discover -s windows_tests  # GUI tests (Windows)
```

The tests use only simulated cartridges and temporary files. They never touch a real reader or any existing ROM.

## Building the distribution ZIPs

The scripts create the `dist/` folder automatically. It receives each ZIP and a `.sha256` file that holds its SHA-256. `dist/` is not included in Git, so attach the ZIPs to GitHub Releases when you share them.

| Command | Produces | Preparation |
|---|---|---|
| `python tools/build_mister_bundle.py` | `…_MiSTer.zip` (for users; bundles pyserial and PyUSB in `vendor/`) | Run it in an environment where `pip install -r requirements.txt` has been done |
| `python tools/build_windows_bundle.py` | `…_Windows.zip` (for users) | None |
| Add `--validation` to either command | `…_Validation.zip` (for verification; includes the development sources and the full test suite) | Same as above |

See [Distribution](docs/DISTRIBUTION.md) for the differences between the ZIPs and how to choose one.

## Documentation

| Document | Contents |
|---|---|
| [USER_GUIDE.md](USER_GUIDE.md) | User guide in English: installing, using, and configuring (included in the distribution ZIPs) |
| [README.ja.md](README.ja.md) | User guide in Japanese (included in the distribution ZIPs) |
| [CHANGELOG.md](CHANGELOG.md) | Change history |
| [docs/README.md](docs/README.md) | Index of the design documents |
| [SYSTEM_FC_NES.md](docs/SYSTEM_FC_NES.md) | FC/NES reading and the mapper support table |
| [GB_GBC](docs/SYSTEM_GB_GBC.md) · [GBA](docs/SYSTEM_GBA.md) · [MD](docs/SYSTEM_MD.md) · [PCE](docs/SYSTEM_PCE.md) · [SFC](docs/SYSTEM_SFC.md) | Per-system design |
| [LICENSES.md](docs/LICENSES.md) | Licenses of dependencies and the references behind the bundled DBs |
| [docs/ja/](docs/ja/README.md) | The design documents in Japanese |

## License

The original code and documents of this project are provided under the **GNU General Public License v3.0** (GPL-3.0-only). See [LICENSE](LICENSE) for the full text.
Dependencies: pyserial (BSD-3-Clause), PyUSB (BSD-3-Clause), and PyQt6 (GPL-3.0 / commercial). The full license texts of pyserial and PyUSB are in [licenses/](licenses). PyQt6 and the Qt-related packages are installed by the user with pip. See [LICENSES.md](docs/LICENSES.md) for how each dependency is handled.
