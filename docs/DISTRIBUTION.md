# Distribution design

[日本語](ja/DISTRIBUTION.md)

## Kinds of packages

| ZIP | For | Contents |
|---|---|---|
| Cartridge_Station_for_RetroFreak_MiSTer.zip | Normal use on MiSTer | The app, standard DBs, setting examples, the launch script, the design documents, the licenses of the dependencies, and vendor (pure-Python sources of pyserial and PyUSB). Does not include the Windows GUI or PyQt6 |
| Cartridge_Station_for_RetroFreak_Windows.zip | Normal use on Windows | The MiSTer contents without vendor, plus the Windows GUI, requirements-windows.txt, and the Windows launcher |
| Cartridge_Station_for_RetroFreak_MiSTer_Validation.zip | Running every test, diagnostics, and development on MiSTer | A self-contained set: the same app, DBs, and vendor as the normal package, plus the Windows GUI, the Windows launcher, requirements-windows.txt, tools, tests, fixtures, and windows_tests (the GUI tests need the Windows dependencies to be installed) |
| Cartridge_Station_for_RetroFreak_Windows_Validation.zip | Tests, diagnostics, and development on Windows | The Windows version of the above (without vendor) |

- The normal ZIPs are the main deliverables. The validation ZIPs are optional and are not a prerequisite for normal installation or import.
- A validation ZIP is a complete set, not a difference. Files it shares with the normal package for the same system have identical contents.
- Next to each ZIP is a `.zip.sha256` file with LF newlines (`<SHA-256>  <file name>`).
- The app folder inside a ZIP is Cartridge_Station_for_RetroFreak.
- No exe is built. The Python and library runtime is on the user's side.

## What is and is not included

| Category | Normal | Validation |
|---|---|---|
| src, standard DBs (data/\*.json), setting examples (config/\*.example.json) | Included | Included |
| Entry points (cart_station.py, .sh) and the diagnostic script | Included | Included |
| Windows GUI, .cmd, requirements-windows.txt | Windows version only | Included in both editions |
| vendor (pyserial, PyUSB) | MiSTer version only | MiSTer version only |
| Design documents (English and Japanese), screenshots of the Windows GUI | Included | Included |
| LICENSE, CHANGELOG.md, README.md and USER_GUIDE.md (English), README.ja.md (Japanese), requirements.txt | Included | Included |
| licenses (full license texts of pyserial and PyUSB) | Included | Included |
| tools, tests (including fixtures), windows_tests | Not included | Included |
| ROMs and save data | Not included | Not included |
| Personal settings (config/\*.json that are not .example), logs, runtime | Not included | Not included |
| Virtual environments, caches, backups, dist, and uncategorized research DBs | Not included | Not included |
| Native binaries (.dll, .pyd, .so, .exe) | Not included (excluded by the inclusion patterns; the MiSTer version has an extra extension check) | Not included |

- Included files are chosen with explicit patterns. An extra local file does not get in if it does not match a pattern.
- The Windows GUI (cart_station_windows.py) goes only into the Windows version and the validation packages.
- The MiSTer launcher (.sh) is stored with execute permission.

## Bundled libraries

- The MiSTer version copies the pure-Python sources and dist-info of pyserial 3.5 and PyUSB 1.3.1, installed with pip in the build environment, into `vendor/`. The build environment needs requirements.txt to be installed.
- The system's Python and libusb are not modified. No native libraries are bundled.
- For the Windows version, the user installs requirements-windows.txt (PyQt6, PyQt6-Qt6, PyQt6-sip, pyserial, and pyusb). Qt itself is not bundled.

## Manifest

- A manifest (MiSTer: BUNDLE_MANIFEST.json; Windows: WINDOWS_MANIFEST.json) is placed directly under the app folder in the ZIP.
- Common fields: product, edition, package_kind (runtime or validation), and files (relative path → SHA-256).
- The MiSTer version also has python_syntax (3.9) and external_files (the SHA-256 of the launcher stored separately directly under Scripts). The Windows version has dependencies (requirements-windows.txt) and has neither python_syntax nor external_files.
- At build time, every .py is parsed as Python 3.9 syntax, and the build fails if any does not pass.

## Build steps

Run these at the root of the development source or of a validation ZIP.

```sh
python tools/build_mister_bundle.py
python tools/build_mister_bundle.py --validation
python tools/build_windows_bundle.py
python tools/build_windows_bundle.py --validation
```

- The output goes to dist/. The ZIP is generated first, then the .zip.sha256.
- A source that was only cloned with git has no dist/. To put the tool on MiSTer, build the ZIP with these steps.

## Installation and updates

- MiSTer: transfer the ZIP to `/media/fat/Scripts` and extract it with `unzip -o`. The destination is `/media/fat/Scripts/Cartridge_Station_for_RetroFreak`. Start it from Cartridge_Station_for_RetroFreak in the Scripts menu.
- Windows: extract to any folder, create a virtual environment in that folder, install requirements-windows.txt, and start it with the launcher or `cart_station.py gui`. Do not move or copy an old virtual environment into the new folder.
- Extracting over an existing install only updates the files contained in the ZIP; personal settings, ROMs, and logs are not changed. The only settings a ZIP delivers are examples (\*.example.json).
- Extracting over an existing install does not delete tools and tests that are no longer in the ZIP. Normal startup does not use them.
- To run every test, update with a validation ZIP. Do not run with tests that are no longer in the ZIP left behind (code and tests of different versions get mixed, and the run fails).

## What works from a normal ZIP

- A normal ZIP has no tests, so do not run `unittest discover` where it was extracted.
- Importing, candidate selection, DB search, and the manager screen's preview display work without tools and tests.

## Public source repository

- The published source keeps the development tools and tests.
- .gitattributes:
  - The default is `text=auto`.
  - `*.sh` is LF and `*.cmd` is CRLF.
  - `*.json` is `-text`, so that newline conversion never changes the bytes of the bundled DBs and test fixtures.
  - `*.png` and `*.zip` are binary.
- Do not commit: ROMs and saves (excluded by extension), personal settings (config/\*.json other than the examples), runtime, dist, virtual environments, caches, backups, and uncategorized research catalogs.
- This source includes no CI configuration. For the recommended verification environment and how to run it, see [Test design](TESTING.md).
