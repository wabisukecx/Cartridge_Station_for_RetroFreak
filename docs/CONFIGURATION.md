# Design of settings, names, and layout

[日本語](ja/CONFIGURATION.md)

## Names

| Use | Name |
|---|---|
| Common name | Cartridge Station for RetroFreak |
| Windows screen | Cartridge Station for RetroFreak — Windows Edition |
| MiSTer screen | Cartridge Station for RetroFreak / MiSTer FPGA Edition |
| App folder | Cartridge_Station_for_RetroFreak (shared by Windows and MiSTer) |
| Windows launcher | Cartridge_Station_for_RetroFreak_Windows.cmd |
| MiSTer Scripts launcher | Cartridge_Station_for_RetroFreak.sh |
| Shared CLI | cart_station.py |
| Distribution ZIPs | Cartridge_Station_for_RetroFreak_Windows.zip and Cartridge_Station_for_RetroFreak_MiSTer.zip (validation packages end with _Validation) |

- Python module names, CLI command names, DB IDs and contents, JSON setting names and formats, and the default save locations are fixed. ROMs are never moved, renamed, or deleted.

## Folder layout

The whole picture, including the development sources and files generated at run time. For what the distribution ZIPs contain, see the note below and [Distribution](DISTRIBUTION.md).

```text
Cartridge_Station_for_RetroFreak/
  cart_station.py                 shared entry point (adds src to the search path)
  Cartridge_Station_for_RetroFreak.sh            MiSTer launcher
  Cartridge_Station_for_RetroFreak_Windows.cmd   Windows launcher
  src/                            reading, communication, verification, saving, screens
  data/                           standard DBs (metadata JSON)
  config/                         setting examples (*.example.json) and personal settings
  licenses/                       full license texts of pyserial and PyUSB
  docs/                           design documents (docs/ja/ holds the Japanese originals)
  vendor/                         pure-Python dependencies for MiSTer (pyserial, PyUSB)
  runtime/                        logs, MGL files, and measurements generated at run time (personal environment only)
  tools/ tests/ windows_tests/    for development and validation (not in the normal distribution)
  requirements.txt                shared dependencies
  requirements-windows.txt        Windows dependencies (including PyQt6)
  LICENSE, CHANGELOG.md, README.md, USER_GUIDE.md, README.ja.md
```

- The user guides USER_GUIDE.md (English) and README.ja.md (Japanese) ship in the ZIPs, together with README.md. README.md also serves as the front page of the development sources, so it includes build and test instructions.
- The Windows launcher, requirements-windows.txt, and the Windows GUI are in the Windows ZIP and the validation ZIP. vendor is only in the MiSTer ZIP.
- Launching does not need tools or tests.
- ROMs, personal settings, logs, virtual environments, caches, and backups are not in any distribution.

## Settings files

There are two kinds. Both are JSON in UTF-8 (a BOM is accepted), and `version` is the integer 1.

### Save folders (output_paths.json)

```json
{
  "version": 1,
  "pc":     { "NES": "~/RetroFreakDumps", "GB": "~/RetroFreakDumps", "...": "..." },
  "mister": { "NES": "/media/fat/games/NES", "GB": "/media/fat/games/Gameboy", "...": "..." }
}
```

- System keys: NES, GB, GBC, GBA, MD, PCE, SGX, SFC. Environment keys: pc and mister.
- Defaults:

| System | PC | MiSTer |
|---|---|---|
| NES | ~/RetroFreakDumps | /media/fat/games/NES |
| GB | ~/RetroFreakDumps | /media/fat/games/Gameboy |
| GBC | ~/RetroFreakDumps | /media/fat/games/GBC |
| GBA | ~/RetroFreakDumps | /media/fat/games/GBA |
| MD | ~/RetroFreakDumps | /media/fat/games/MegaDrive |
| PCE | ~/RetroFreakDumps | /media/fat/games/TGFX16 |
| SGX | ~/RetroFreakDumps | /media/fat/games/TGFX16 |
| SFC | ~/RetroFreakDumps | /media/fat/games/SNES |

- GB and GBC have separate save folders because MiSTer's ConsoleMode does not recognize GBC ROMs in the Gameboy folder, but does in the GBC folder.
- Validation: a value is a non-empty string. On MiSTer it is an absolute Linux path (no backslashes). On a PC it is an absolute path or a path that starts with `~/`. A violation is an error; nothing is filled in.
- Even if the settings file has no keys other than NES, the defaults above fill them in (the settings file itself is not rewritten). If the settings file does not exist, that is an error.
- Priority: the save folder chosen on the screen and the CLI's `--output-dir` win. Next is a file named with `--paths-config`, then the personal settings file, and finally the defaults.
- The save folder follows the detected system (it changes for each system inserted). When the save folder is specified on the screen or on the CLI, it does not change.
- The environment (pc or mister) is detected automatically. It is MiSTer only on ARM Linux with the SD card layout (the file /media/fat/MiSTer and the directory /media/fat/linux). `--environment` overrides it.

### MiSTer cores (mister_cores.json)

```json
{
  "version": 1,
  "NES": { "rbf": "_Console/NES", "delay": 2, "index": 1, "type": "f" }
}
```

| System | rbf | index |
|---|---|---|
| NES | _Console/NES | 1 |
| GB, GBC | _Console/Gameboy | 1 |
| GBA | _Console/GBA | 1 |
| MD | _Console/MegaDrive | 1 |
| PCE | _Console/TurboGrafx16 | 0 |
| SGX | _Console/TurboGrafx16 | 1 |
| SFC | _Console/SNES | 1 |

- Common: type is f, and delay is 2 (1–30 seconds).
- index is fixed at 0 for PCE and 1 for the others, and a different value in the settings is rejected. Only rbf and delay can be changed.
- rbf is a path relative to the SD root, without `.rbf` and without a date. Before launching, the tool checks that the actual file (`<name>.rbf` or `<name>_<date>.rbf`) exists.
- If a system's key is missing, the defaults above are used.

### Personal DB (nes_profiles.json)

- A personal DB that replaces the standard FC DB. If it is not present, the standard DB is used. Its format is the FC profiles format in the database design.

## Settings resolution order

1. The personal setting in config/ (for example config/output_paths.json)
2. The example in config/ (config/output_paths.example.json)
- An update never rewrites a personal setting. The ZIPs ship only examples (\*.example.json), and an example never overwrites a personal setting.
- The tool works without any settings file, using the values in the examples. To change them, copy the example to the same place under a name without `.example` and edit the copy.
- The personal DB is config/nes_profiles.json.

## Carrying over settings from the old name's install location

- There is no automatic migration from the install location of the old name (MiSTer Cart Station), and no automatic search of the old folder. Extract into the new app folder; settings are read from the current `config/`.
- To carry over the save-folder and core settings, copy the new `config/*.example.json` to names without `.example`, and copy the values you need from the old settings. Follow the keys and formats in the examples in this document.
- To carry over a personal DB, put one that matches the profiles format in [Database design](DATABASE.md) at `config/nes_profiles.json`.
- To keep using existing ROMs, set their folder as the save folder. The app does not move, rename, or delete ROMs for migration. Recreate the Windows virtual environment in the new location.

## Files generated at run time (runtime/)

| File | Contents |
|---|---|
| manager.log | Detailed log of the MiSTer manager screen (including communication error details) |
| last_capture_metrics.json | Measurements of the most recent import |
| \<system\>_\<raw SHA-1\>_\<header hash\>.mgl | The MGL for launching a game |

- On MiSTer, if the running app's folder is the app folder directly under Scripts, its runtime is used; otherwise the runtime of Scripts/Cartridge_Station_for_RetroFreak is used.

## Launch scripts

- MiSTer (.sh): looks for the app in the app folder directly under Scripts and then in the folder of the script itself, sets `TERM=linux`, and runs `python3 cart_station.py manager`. If it finds neither, it prints a message and exits.
- Windows (.cmd): changes to the script's folder. If `.venv\Scripts\python.exe` is missing, it explains how to create the environment and stops. Otherwise it runs `cart_station.py gui`.
- Diagnostic script (cart_station_check.sh): shows system information, whether Python, pyserial, and PyUSB are present, USB descriptors, sysfs, and serial nodes. It sends no command to the reader and does not change any driver.

## Dependencies

| Name | Version | Placement |
|---|---|---|
| pyserial | 3.5 | Bundled in vendor on MiSTer; pip on Windows |
| PyUSB | 1.3.1 | Bundled in vendor on MiSTer; pip on Windows |
| PyQt6 / PyQt6-Qt6 / PyQt6-sip | 6.11.0 / 6.11.2 / 13.13.0 | Windows only. Installed with pip; not bundled |
| libusb | system | Not bundled. Uses MiSTer's existing libusb-1.0.so.0 |

- If vendor exists, it is put at the front of the search path.
- The Python that ships with MiSTer may lack the Japanese codecs, so Shift-JIS conversion is not required. When a title cannot be converted, an ASCII substitute is used; the byte sequence used for identification and the ROM hashes do not change.
