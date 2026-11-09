# Design of import, verification, and saving

[日本語](ja/CAPTURE_FLOW.md)

## Responsibilities of the service layer

- The Service owns one transport, the candidates, the selection, the save folder, and the checks of verified files, all in one worker. It does not depend on Qt or a terminal.
- The main operations are as follows.

| Operation | Meaning |
|---|---|
| poll | If not connected, open the transport and get the product information. Get the cartridge state, and once it is stable, identify it and return a state snapshot |
| select_profile | Choose one of the current candidates. If the candidates have gone, ask for a re-check |
| retry | Close the connection, discard the caches, and poll again from the beginning |
| capture | Read the selected candidate (or the automatic-decision group), verify it, and save it |
| existing | Return the saved ROM for the selected candidate, verified by the full-ROM hash |
| prepare_game | For MiSTer. Re-verify the saved ROM, build the MGL, and return the launch information |
| close | Release the transport and the state |

- The state snapshot (PollState) holds the reader state, the cartridge text, whether import is possible, the candidate list, the selected candidate, whether a saved file exists, whether a selection is pending, the number of automatic-decision candidates, and the save folder. The screens draw from this alone.
- If any of poll, capture, or prepare_game raises an exception, the service closes the transport and passes the exception on. The screen waits for the user to reconnect explicitly.

## Insertion detection

- Until the same cartridge state has been obtained three times in a row, "waiting for a stable state" is shown and nothing is identified. Every time the value changes, the candidates and the selection are discarded.
- Empty (0 or 255), several inserted (254), and a system that is out of scope each stop with a separate message.
- The same insertion is not identified repeatedly. Removing and reinserting, reconnecting, and retrying identify it again.

## Identification per system

| System | How it is identified | What is fixed before import |
|---|---|---|
| FC/NES | Take partial hashes (the first 1 KiB, the last 1 KiB, and so on) from the initial PRG and match them against the DB's partial-hash indexes to build candidates (no writes, no bank switching) | Candidates only. The title is confirmed after the full-ROM hash is matched |
| GB/GBC | Read the header (80 bytes from 0x100) several times and verify the logo, checksum, type, and capacity code | The title in the header, the MBC, the capacity, and the GB/GBC class |
| GBA | Read the first 2 KiB twice independently and verify the fixed value 0x96 and the header checksum | The title, game code, and revision. The capacity is not yet fixed |
| MD | Read 0x100–0x1FF and parse the SEGA identifier and the ROM range | The title, serial, read method, and a candidate capacity |
| PCE | Decide the method, read the whole ROM once, and match the full-ROM hash against the DB | The official name, capacity, data order, and the PCE/SGX class |
| SFC | Read the 48 bytes at $00:FFB0 several times and verify the checksum, map mode, and capacity code | The title, method, chip, and an upper-bound candidate capacity |

## Choosing a candidate

- One candidate: it is selected automatically.
- Several candidates that are all readable and have the same mapper, submapper, PRG capacity, and CHR capacity (except Mapper 206): they form an "automatic-decision group" that proceeds without a manual choice. The ROM is saved under that title only if the PRG, CHR, and full SHA-1 after reading match exactly one candidate.
- Any other set of several candidates: the user chooses. A set is treated this way if even one unsupported candidate is mixed in.
- Whether each candidate is supported is shown with a reason per candidate. Choosing an unsupported candidate disables import and shows the reason.
- A candidate in the list means "the reader supports it". It is not a guarantee that the title and revision match the real cartridge. A partial hash can match other titles and revisions, so the title and revision are treated as unconfirmed until the full-ROM hash matches. The ROM is saved only if it matches the full-ROM hash of the chosen title.

## Import (capture) procedure

1. Confirm that the candidate (or the automatic-decision group) is selected and supported.
2. Re-identify the cartridge just before reading, and confirm it matches the candidate at selection time. If it changed, stop.
3. Run two independent full-ROM reads using each system's procedure. Progress is reported in the stages "Read 1/2" and "Read 2/2".
4. Check that the two reads match, the header, and the per-system conditions.
5. Match against the DB's full-ROM hash. A mismatch, a ROM not in the DB, or a SHA-256 mismatch is not saved and stops.
6. Create the save folder and save the verified data.
7. Read the saved file back and verify it again against the DB.
8. Return the saved path and whether it was newly created or an existing file was reused.
- The shutdown step (restoring the mode) is attempted whether the read succeeds, fails, or is cancelled.
- Cancellation happens at a safe boundary (a checkpoint). Once a save write has begun, finishing it takes priority. If it is cancelled before saving, no new ROM is saved.

### Layers of verification

"Independent" means getting the whole range again without reusing a cache or the first read's data. A USB reconnection, a power cycle, or a hardware reset of all internal state is not guaranteed.

| System | State at the start of the second read |
|---|---|
| FC/NES | A fixed layout is re-read from the beginning. For mapper methods, the read procedure's initialization and bank setup are run again. The same mode connection is used |
| GB/GBC | The ROM register setup for each bank is run again, and the data is read from the beginning. The same mode connection is used |
| MD | The ROM mapping setup for the method is run again, and the data is read from the beginning. The same mode connection is used |
| PCE | The address specification of each block and, for the methods that need it, the bank setup are run again. The same mode connection is used |
| GBA | 04(0), 05, a 200 ms wait, a state check, receiving a complete first 2 KiB and confirming the header matches, then reading from the beginning |
| SFC | After the shutdown step: a state check, initialization, and reconfirming that the stable header and method match. The dedicated ROM mapping is also set again, and the data is read from the beginning |

For GBA and SFC, a mismatch, an incomplete reply, a removal, or an initialization failure right before the second read is not saved and stops. The limited retry for an invalid SFC header applies only at identification and at the start of the first read, not right before the second read.

Every system is registered in the service's `SYSTEMS`, and `poll` and `capture` are called through a common entry point. The header-centered systems share DB loading and import through `HeaderSystem`, and NES uses `NesSystem` to handle several candidates, the automatic-decision group, and re-identification just before writes. The check of the selection happens before communication, and an exception during an import closes the connection through the shared handling.

Saving is gathered in the service's `_save_capture`. For all systems it does publishing, conflict avoidance, and the re-check after saving, and it updates the selected profile only after success.

| Layer | Contents |
|---|---|
| Communication | The reply header, the received length, and the trailing state reply (the system matches) |
| Two independent reads | The whole ROM is read twice, and the byte sequences must match exactly |
| Header | The per-system header, logo, checksum, and range |
| Partial verification | For FC, the SHA-1 of the PRG, the CHR, and PRG+CHR |
| Full-ROM hash | The full SHA-1 in the DB. If a SHA-256 is recorded, it must match as well |
| After saving | The saved file is read back and verified again |

- Neither FF regions nor mirrors are used to guess a shorter capacity. The capacity is fixed by the full-ROM hash at each size recorded in the DB.

## Saving

### File names

- The official name in the DB is turned into a safe file name. The extension depends on the system: .nes (with the 16-byte iNES header), .gb, .gbc, .gba, .md, .pce, .sgx, and .sfc (no copier header).
- The name without the extension is truncated to 170 bytes in UTF-8, so that even with the hash-qualified alternate name (`.verified-<SHA-256>`) one name never exceeds 255 bytes. The longest title in the bundled DBs fits within this limit, so existing saved names do not change.
- MD saves the received bytes as they are. It does no 16-bit swap, no SMD interleave, and does not add a 512-byte header.
- PCE decides the data order (the received order, or a per-byte bit reversal) by DB matching, and saves the normalized data.

### Publishing from a temporary file

1. Exclusively create `.rfdump-<random>.tmp` in the same directory as the save location.
2. Write the data, then flush and fsync.
3. Publish it under the final name in a way that does not replace an existing file.
4. Delete the temporary file and sync the directory (best effort, because it is not available in every environment).
- How it is published: on Windows, os.rename (which fails if the file exists). On Linux, renameat2 with RENAME_NOREPLACE. On 32-bit ARM (EABI), where the library lacks the function, it calls system call number 382 directly. If that is not usable either, it tries the hard-link method, and on storage that cannot support it, it fails safely.
- Old temporary files (more than an hour old) are cleaned up on every save. Only temporary files that follow its own naming rule are targeted.
- An incomplete write stays under the temporary name and is never published under the ROM name.
- fsync and directory sync do not guarantee that data survives a power loss on every SD card.

### Handling conflicts

| Situation | Behavior |
|---|---|
| An existing file with the same name and content | Reuse it (nothing is newly created) |
| An existing file with the same name and different content | The existing file is not moved, deleted, or overwritten; the verified data is saved as `<original name>.verified-<SHA-256>.<extension>` |
| The alternate name also exists with different content | The save is treated as a failure |
- The screen says that the existing file was left in place.
- When an alternate-named file is reused or launched, the hash in its name is not trusted; the full-ROM hash is recomputed and verified.

### Finding an existing ROM

- For the selected candidate, the tool looks for a file that can be verified by the DB's full-ROM hash, in this order:
  1. The file with the official name in the save folder
  2. A `.verified-` alternate-named file in the save folder
- Verification results are cached, keyed by path, size, and modification time. Right before a game is launched, the cache is not used and the file is verified again.

## Launching a game (MiSTer)

1. Confirm GAME START on the screen.
2. The manager screen releases the reader and re-verifies the saved ROM of the selected title (header, PRG, CHR, and the whole).
3. If the ROM is outside the save folder (a compatible save location), a verified copy is saved into the save folder.
4. Read the system's core setting and confirm that the core's RBF exists on the SD card.
5. Create the MGL (MiSTer Game Description) in the app's runtime folder, through a temporary file. Its content is `rbf` (the core) and `file` (delay, type, index, and the ROM's absolute path). The file name is `<system>_<raw SHA-1>_<first 8 digits of the header hash>.mgl`.
6. Write one line, `load_core <absolute path of the MGL>`, to `/dev/MiSTer_cmd` without blocking. If it is not a FIFO or nobody is listening, it is an error and the screen returns. The command has no control characters and is at most 1000 bytes.
- Validation of the core setting: rbf is a path relative to the SD root without the .rbf extension. An absolute path, `..`, a backslash, and control characters are not allowed. type is f, index is 0 for PCE and 1 for the others, and delay is 1–30 seconds.
- Returning after the game ends is left to MiSTer (Reboot). There is no resident feature that detects the end of the game and shows the manager screen again.

## Measuring import time

- At the end of an import (saved, identical existing, cancelled, or error), only the most recent one is recorded in `runtime/last_capture_metrics.json`. A failure to write does not change the ROM's save result (the record is replaced through a temporary file, and a failure is swallowed).
- Contents of the record:

| Item | Meaning |
|---|---|
| version / scope | The format version (1) and scope (capture_only) |
| outcome / error_type | saved, identical, cancelled, or error, and the exception name |
| elapsed_seconds | Seconds from the start of the import to the end of the shutdown step (does not include writing the record file) |
| phases | Seconds and communication totals for each stage reported to the screen |
| io | The number of read/write calls on the transport, the bytes that succeeded, the number of exceptions, and the seconds taken (the whole call as seen from Python, including the wait for the driver) |
| operation_seconds | Seconds spent on the DB loading needed during the import (load_\*_database), FC's last-minute candidate check (identify_nes), the save function (save_verified_rom), and the post-save verification (verify_saved_file) |
| system / transport / reader / rom_bytes | The system, the transport's class name, build/product, and, on success, the ROM body size (excluding FC's 16-byte header) |
| recorded_utc / python / platform / machine | The recording time (UTC) and the execution environment |
| bundle_manifest_sha256 | The SHA-256 of the distribution manifest (null if there is none) |
- io and operation_seconds are breakdowns of phases and elapsed_seconds; adding them together would count them twice.
- Measurement only wraps the transport calls; it does not change the content or order of communication. The data received is not kept.
- Most of the import time is taken by transport reads and writes, so reducing the number of communications and the amount transferred helps more than optimizing computation.
