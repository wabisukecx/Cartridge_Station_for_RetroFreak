# Design of licenses and references

[日本語](ja/LICENSES.md)

## License of this project

- The original code and documents of this project are GPL-3.0-only. The full text is in LICENSE at the repository root, and it is included in every ZIP.
- PyQt6, which the Windows GUI uses, is dual-licensed under GPLv3 or a commercial license, so this project follows the GPLv3 terms.

## Dependencies

| Item | How it is distributed | Terms |
|---|---|---|
| pyserial 3.5 | Pure-Python source bundled in vendor of the MiSTer edition; pip on Windows | BSD-3-Clause. The full text is in licenses, and the copyright notice, conditions, and disclaimer are kept |
| PyUSB 1.3.1 | Pure-Python source bundled in vendor of the MiSTer edition; pip on Windows | BSD-3-Clause. The full text is in licenses, and the original text inside vendor is kept |
| PyQt6 6.11.0 | Installed by the user with pip on Windows. Not bundled in the ZIPs | GPL-3.0-only (dual: GPLv3 or commercial) |
| PyQt6-Qt6 6.11.2 | Same as above. No binaries are bundled | LGPL v3. Check the terms of each module if you redistribute it |
| PyQt6-sip 13.13.0 | Same as above. No binaries are bundled | BSD-2-Clause |
| Python, libusb | The ones in the user's environment are used. Not bundled | If they are ever bundled, record the version and terms separately |

- The full texts kept in `licenses/` are the two for pyserial and PyUSB. For the licenses of PyQt6 and the Qt-related packages, refer to the ones that come with each package installed with pip.
- Not building an exe and meeting the terms of the libraries in use are treated as separate matters.
- No binaries or fonts are bundled.

## References used

- The mapper specifications were implemented in this project with reference to the NESdev Wiki, the mapper information of the gamebank.jp dumper, and the mapper handling of Mesen (GPL v3). The list is in "References used" in [DATABASE](DATABASE.md).
- Mesen is licensed under GPL v3, which is compatible with this project's GPL-3.0-only. If Mesen source code is ever incorporated, its copyright notices are kept.

## Data policy

- ROM data and save data are included in neither the repository nor the ZIPs.
- The purpose of this tool is to back up cartridges the user owns; it is not for distributing anyone else's works.

## Names and trademarks

- "RetroFreak" (and its Japanese name) are names of their respective rights holders. This tool is unofficial: it uses the cartridge adapter of those products as a reader and has no relationship with the rights holders.
- Names such as "CYBER Gadget", "MiSTer", "Nintendo", "SEGA", and "NEC" are used only to indicate supported systems and compatibility.
- Product names are limited to descriptive use that identifies the hardware supported.

## Open items

- The range that has been confirmed by reading on real hardware. The confirmed range is recorded separately in [HARDWARE_VALIDATION](HARDWARE_VALIDATION.md).
