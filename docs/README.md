# Design documents

[日本語](ja/README.md)

A set of documents that lays out the design of Cartridge Station for RetroFreak, one topic at a time. Each document can be read on its own, with the facts it needs written in it.

## Start here

| What you want to know | Document to read |
|---|---|
| The big picture and design principles | [OVERVIEW](OVERVIEW.md) |
| How to use it (screens and commands) | [FRONTEND_WINDOWS_GUI](FRONTEND_WINDOWS_GUI.md), [FRONTEND_MISTER_MANAGER](FRONTEND_MISTER_MANAGER.md), [CLI](CLI.md) |
| Getting, installing, and updating the distribution ZIPs | [DISTRIBUTION](DISTRIBUTION.md), [CONFIGURATION](CONFIGURATION.md) |
| Reading a specific system | "Per-system design" below |
| Development, testing, and hardware validation | [TESTING](TESTING.md), [HARDWARE_VALIDATION](HARDWARE_VALIDATION.md) |
| Licenses and the references behind the data | [LICENSES](LICENSES.md), [DATABASE](DATABASE.md) |

## Common design

| Document | Contents |
|---|---|
| [OVERVIEW](OVERVIEW.md) | Purpose, design principles, what is out of scope, environment, layers, and the overall import procedure |
| [PROTOCOL](PROTOCOL.md) | The reader, the transports (serial and USB), packets, commands, state codes, and error design |
| [CAPTURE_FLOW](CAPTURE_FLOW.md) | The service layer, identification and candidate selection, two reads and verification, saving, game launch, and measuring import time |
| [DATABASE](DATABASE.md) | The format of the bundled DBs, what they contain, matching, and the references and update policy |
| [CONFIGURATION](CONFIGURATION.md) | Names, folder layout, settings files, resolution order, and dependencies |
| [DISTRIBUTION](DISTRIBUTION.md) | The kinds and contents of the distribution ZIPs, building, installation and updates, and running the repository |

## Per-system design

| Document | Contents |
|---|---|
| [SYSTEM_FC_NES](SYSTEM_FC_NES.md) | FC/NES: identification, the structure of reading, and per-mapper conditions and design notes |
| [SYSTEM_GB_GBC](SYSTEM_GB_GBC.md) | GB/GBC: the header, per-MBC reading, and classification diagnostics |
| [SYSTEM_GBA](SYSTEM_GBA.md) | GBA: the header and fixing the capacity with the DB |
| [SYSTEM_MD](SYSTEM_MD.md) | Mega Drive: the header and read methods |
| [SYSTEM_PCE](SYSTEM_PCE.md) | PC Engine / SuperGrafx: deciding the method, data order, and identification on insertion |
| [SYSTEM_SFC](SYSTEM_SFC.md) | SFC: the header, limited retry for an invalid header, and read methods |

## Screens and commands

| Document | Contents |
|---|---|
| [FRONTEND_WINDOWS_GUI](FRONTEND_WINDOWS_GUI.md) | The PyQt6 GUI of the Windows edition |
| [FRONTEND_MISTER_MANAGER](FRONTEND_MISTER_MANAGER.md) | The text menu for TV and gamepad of the MiSTer edition |
| [CLI](CLI.md) | The command-line subcommands and their behavior |

## Quality and rights

| Document | Contents |
|---|---|
| [TESTING](TESTING.md) | The structure of the tests and what they check |
| [HARDWARE_VALIDATION](HARDWARE_VALIDATION.md) | The open scope of hardware validation, priorities, and the evidence to record |
| [LICENSES](LICENSES.md) | This project's license, dependencies, data policy, names and trademarks, and the reference to hardware validation |
