# Database design

[日本語](ja/DATABASE.md)

## Policy

- The bundled DBs hold **only metadata**: titles, sizes, hashes, headers, and so on. They contain no ROM content.
- The DB is the basis for verification by full-ROM hash, and is separate from the range the read implementation supports. A cartridge listed in the DB still cannot be imported if it does not fit the read conditions. Being listed in the DB does not mean it can be read on real hardware or that the game runs.
- The JSON files are bundled as they are and loaded at startup. They are not embedded in an executable and their contents are not hidden. Users can inspect and replace them.
- The target is cartridges that were commercially released for that system. Recent new releases, reissue-only releases, homebrew, hacks, translation patches, pirated copies, and unreleased prototypes are not added to the standard DBs.
- Every DB has `version: 1`, and loading is refused if the version differs. If any record is invalid, the whole DB is refused; it is never loaded partially.

## Files

| File | System | Entries | Format |
|---|---|---:|---|
| nes_profiles.reference.json | FC/NES | 2,148 (71 mapper numbers) | profiles |
| gb_profiles.reference.json | GB/GBC | 3,455 | records |
| gba_profiles.reference.json | GBA | 3,154 | records |
| md_profiles.reference.json | MD | 1,969 | records |
| pce_profiles.reference.json | PCE/SGX | 409 (PCE 404, SuperGrafx 5) | records |
| sfc_profiles.reference.json | SFC | 3,693 | records |

- The counts are numbers of profiles. They are not numbers of distinct game titles or of cartridges confirmed on hardware.

## FC/NES DB (profiles format)

### Top level

| Key | Meaning |
|---|---|
| version | 1 |
| profiles | Array of profiles (up to 100,000) |
| import_skipped | Records that could not be imported, with the reasons |
| summary | Counts |

### Profile

| Key | Meaning |
|---|---|
| id | Unique ID. Normally `<raw SHA-1>-m<mapper>-s<submapper>`. A duplicate is an error |
| title | Display name (up to 240 characters, no control characters). When used as a file name, unsafe characters are replaced, and a reserved name becomes `NES_<raw SHA-1>` |
| mapper / submapper | Mapper number (0–4095) and submapper (0–15) |
| prg_size / chr_size | Bytes of PRG / CHR ROM |
| header | 16-byte iNES / NES 2.0 header (hex). No trainer |
| prg_sha1 / chr_sha1 | SHA-1 of PRG and CHR (may be omitted when there is no CHR) |
| raw_sha1 | SHA-1 of PRG+CHR |
| file_sha1 | SHA-1 of the finished file with header (optional) |
| probes | Partial hashes. Any of first1k_sha1, last1k_sha1, halflast1k_sha1, and lastprev16k_1k_sha1 |
| prg_crc32 | CRC32 of the PRG (optional; used to choose the unlock values for CNROM-CP) |

- On load, the tool verifies that the mapper, submapper, and PRG/CHR capacities in the header match the profile's values. A mismatch is refused.
- Unsupported header formats (a trainer, exponent-form capacities, console-type or extension bits) are refused on load or treated as unsupported.

### Candidate search (identification)

- On the initial PRG state, the tool does a 1 KiB dummy read from 0x8000, then reads 1 KiB from 0x8000 (the head) and 1 KiB from 0xFC00 (the tail). It does no writes and no bank switching.
- The head's SHA-1 is looked up in the indexes for `first1k_sha1` and `lastprev16k_1k_sha1`, and the tail's SHA-1 in the indexes for `last1k_sha1` and `halflast1k_sha1`. The union of the two results is the candidate set (the union of the four indexes).
- A candidate is only a partial match. It is not a confirmed title.

### Support decision (support)

- If the header has console-type bits or extension fields, the cartridge is unsupported.
- Mapper 0 (submapper 0, PRG 16/32 KiB, CHR 0/8 KiB) is supported.
- For a supported mapper number, the cartridge is supported if it fits that mapper's read conditions (submapper and capacity ranges). If it does not fit, the reason is returned.
- Everything else is "mapper/layout not supported".
- Every profile in the bundled DB fits the current read conditions.

### Load order

1. A file given explicitly with the CLI's `--db` or on the screen
2. The personal DB (config/nes_profiles.json)
3. The standard DB (nes_profiles.reference.json)
- The personal DB replaces the standard DB (they are not merged). Unclassified supplementary catalogs are not merged automatically.

## Linear-ROM DBs (records format)

Common to GB/GBC, GBA, MD, PCE/SGX, and SFC.

| Key | Meaning |
|---|---|
| system | One of GB, GBC, GBA, MD, PCE, SFC (fixed per DB) |
| title | Official name (with extension) |
| size | Bytes of the ROM |
| sha1 | Full-ROM SHA-1 (40 lowercase hex digits) |
| sha256 | Full-ROM SHA-256 (optional; if present, it is required to match) |
| platform | PCE DB only: PCE or SGX |

- Validation: the system matches; the size is an even integer within the range for the system; the SHA-1 has the right format; and the title is not empty. If one (size, SHA-1) has different contents, it is rejected as a conflict. If a SHA-256 is present, its format is also validated.
- Matching uses an index of (size, SHA-1). If the matched record has a SHA-256, the data's SHA-256 must also match. A mismatch is a DB integrity error and is not accepted as a classification difference.
- Size ranges:

| System | Min | Max | Note |
|---|---|---|---|
| GB/GBC | 32 KiB | 8 MiB | Out-of-range records are excluded when the DB is generated |
| GBA | 192 bytes | 32 MiB | |
| MD | 512 bytes | 16 MiB | Even length |
| PCE/SGX | Sizes recorded in the DB | Limit per method (Linear 1 MiB, Interval 512 KiB, Sf2Dash 8.5 MiB) | |
| SFC | 32 KiB | 16 MiB | |

- Records that duplicate the same capacity and SHA-1 are made unique in title order.
- For GB/GBC, the full match uses the header's classification (GB/GBC). Only if that fails does the tool look for a record of the same capacity and SHA-1 in the other classification. If the full-ROM hash (and the SHA-256, when recorded) matches in the other classification, the tool stops with the diagnostic "the header's classification and the DB's classification differ". It does not save the ROM, automatically reclassify it, or change the extension or core. If the same ROM exists in both classifications, a match with the header's classification wins and the ROM is saved as usual.

## Update policy

- The bundled DBs are data fixed at the time of distribution, and the repository does not include a tool to regenerate them.
- Result of importing the FC standard DB: 2,148 profiles and 71 mapper numbers. Records with no counterpart on the partial-hash side (about 8,250), PRG/CHR contradictions, and unsupported mirroring formats are recorded with reasons in `import_skipped`.
- Records with different board information are kept as separate candidates, and collisions of the same partial hash are not forcibly resolved.

### What the FC standard DB includes

- The Mapper 206.1 profile keeps its ID, display name, and finished-file hash fixed (for verifying the fixed layout).
- `prg_crc32`, which is needed to choose the unlock values for CNROM-CP (Mapper 185), is added to those 10 entries only.
- Supplementary data that has only full-ROM hashes (on the order of 7,058 entries) cannot be identified by partial hashes and does not fit the policy of limiting the DB to commercial releases, so it is not included in the standard DB.

## References used

- The procedures for mapper registers and bank switching were implemented independently in this project, with reference to publicly available technical documents.
  - NESdev Wiki (https://www.nesdev.org/wiki/): specifications of each mapper and of the NES 2.0 header
  - gamebank.jp dumper (http://www.gamebank.jp/dumper/): mapper information (such as the Mapper 185 unlock values)
  - Mesen (https://github.com/SourMesen/Mesen2, GPL v3): the mapper handling of the emulator
- No one else's scripts or source code are included in the repository.
- This project's license (GPL-3.0-only) is not applied automatically to third-party documents or data.
