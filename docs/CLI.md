# Command-line design

[日本語](ja/CLI.md)

## Entry point

- `cart_station.py` is the only entry point. It puts `src` at the front of the search path, puts `vendor` (if present) in front of that, and runs the CLI.
- An error at exit is printed to standard error as `Stopped: <message>`, and the exit code is 1.
- `cart_station.py` exists only for launching. To use or test from Python, import the implementation modules directly (`cart_station_cli`, `cart_station_transport`, `cart_station_protocol`, and so on). The old entry point does not re-export them, and `sys.modules` is not replaced.

## Common options

| Option | Meaning |
|---|---|
| `--port <port>` | Serial port (COM3, /dev/ttyACM0, and so on). Mutually exclusive with `--usb` |
| `--usb` | Direct USB communication (for MiSTer and the CLI; the Windows GUI does not accept it) |
| `--environment auto\|pc\|mister` | Override environment detection (the default is automatic) |
| `--paths-config <file>` | Specify the save-folder settings file explicitly |

## Subcommands

| Command | Meaning |
|---|---|
| `list` | List serial ports (with VID/PID). Does not open the reader |
| `probe` | Check the product information and cartridge state (does not read a ROM) |
| `gui` | The PyQt6 GUI for Windows. Not available on MiSTer. Accepts `--output-dir` and `--db` |
| `manager` | The TV/gamepad screen for MiSTer. `--output-dir`, `--core-config`, `--db`, and `--preview` (show the screen without opening hardware) |
| `list-db` | Search the FC DB (does not connect). `--db`, `--query`, `--mapper`, `--limit` (1–1000) |
| `identify-db` / `dump-db` | Identify FC candidates, and import with confirmation. `dump-db` takes `--profile <ID>` (choose among the identified candidates) or `--catalog-profile <ID>` (state a DB read plan explicitly even without a partial match; full verification is mandatory), and `--output-dir` |
| `identify-gb` / `dump-gb` | Check the GB/GBC header, and import. `--gb-db`. `--output-dir` is for `dump-gb` only |
| `identify-gba` / `dump-gba` | GBA. `--gba-db`. `--output-dir` is for `dump-gba` only |
| `identify-md` / `dump-md` | MD. `--md-db` and `--controller None\|SAndN\|SuperSf2\|MortalK2`. `--output-dir` is for `dump-md` only |
| `probe-pce` / `identify-pce` / `dump-pce` | PCE/SGX. `--pce-db` and `--controller Linear\|Interval\|Sf2Dash`. `--output-dir` is for `dump-pce` only |
| `identify-sfc` / `dump-sfc` | SFC. `--sfc-db`. `--output-dir` is for `dump-sfc` only |
| `diagnose-gb` | Diagnose the GB header (three reads). `--reset` and `--trace <new file>` |

- Every import command asks for confirmation (y) on an interactive terminal. Before the confirmation it reads the header or partial hashes; after it, it starts the two independent reads for saving. `identify-pce` reads the whole ROM once without saving and matches it against the DB.
- FC import, including Mapper 0 (NROM), is integrated into `dump-db`. The old commands (`dump-nrom` and `watch-nrom`), which let the user specify the capacity and mirroring and saved without DB matching, have been removed.
- The output folder is decided in this order: `--output-dir`, the per-system save folder in the settings file, then the default.
- Saving uses the shared save code, and a file with the same name and different content is saved under an alternate name. After saving, the file on disk is checked again and "Saved and verified" or "Already saved (identical)" is printed. GB/GBC, GBA, MD, PCE, and SFC also print the SHA-1.

## Output format

- Identification commands print the result as JSON (state, system, title, rom_size, readable, limitation, and so on).
- Progress and retry counts go to standard error and are not mixed into the JSON output.

## Use from Python

- `cart_station_cli` handles command-line processing. It is not a compatibility module that re-exports the old API in one place.
- Import communication from `cart_station_transport`, shared constants from `cart_station_protocol`, the DB from `cart_station_nes_db`, and per-system reading from each implementation module directly.
