# Test design

[日本語](ja/TESTING.md)

## Policy

- The basic approach is to verify with synthesized communication and ROMs, without a real reader or a real cartridge. Normal tests do not open hardware.
- A passing test, a successful import of a real cartridge, and the game running are treated as separate facts and never conflated.
- The normal dump code is not changed to suit old tests. Hash matching is never skipped.
- For the range not yet verified on hardware, the priorities, and the conditions for recording a success, see [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md).

## Layout

| Location | Contents | Environment |
|---|---|---|
| tests/ | Shared tests: communication, per-system reading, DB, saving, settings, the manager screen, and the distribution layout | Python 3.9 or later. No PyQt6 needed |
| tests/fixtures/ | Reference traces (JSON) | Same |
| windows_tests/ | Windows GUI. Uses a simulated cartridge and Qt's offscreen mode | Needs PyQt6 |
| tools/diagnose_status.py | Diagnostic that records the raw replies to the cartridge state (for hardware) | A real reader |

Running:

```sh
python -m unittest discover -s tests -q
python -m unittest discover -s windows_tests -q     # Windows GUI; QT_QPA_PLATFORM=offscreen
```

- On MiSTer, run with a location for temporary files: `TMPDIR="$PWD" python3 -m unittest discover -s tests -q`.
- The normal ZIPs do not include tests. Run the tests from a validation ZIP or from the root of the source.
- This source includes no CI configuration. The recommended verification environment is the shared tests on Python 3.9 and 3.12 and the GUI tests on Windows; this is kept distinct from results that have actually been run.

## What is verified

### Communication and transport

- Packet assembly, reply headers, reception split into single bytes, partial reception and timeouts, no reply, invalid headers, and a mismatch in the trailing state reply.
- The byte sequences sent for 04/05, and the order of communication for NES reads.
- State codes (0 and 255 for empty, 254 for several inserted, undefined values), the three-in-a-row stability rule, and insertion and removal, reconnection, and prevention of double imports.
- USB (CDC control, endpoint selection, and handling of alternate settings), the limits of input recovery (time, bytes, and count), and continuing after recovery.
- Exception chains and error messages for send and receive failures.

### Comparison with reference traces

- The reference traces (fixtures), obtained by running each bank-switching procedure on a synthetic bus, are compared with the Python read addresses, sizes, and bank-write order.
- Places that intentionally differ from a reference trace (corrected initialization, added protective writes, restricted ranges) are verified separately, with the position of the difference stated explicitly.
- The comparison uses a model that imitates address decoding, bank registers, and window switching independently. It confirms that the data read is reconstructed into the expected ROM.

### Per-system reading

- Reconstruction of every bank, the maximum capacity, boundary banks, the lower and upper capacity limits, submappers, and that an unsupported layout stops before any communication.
- Corruption in the second read; mismatches in the PRG, CHR, and full-ROM hashes; a ROM not in the DB; a SHA-256 mismatch; header changes; cartridge swaps; disconnection; and cancellation.
- That a method that failed is not retried unconditionally with another method (for an allowed limited retry, the upper bound on attempts is verified).
- Restrictions on the addresses and values that may be written (rejection of forbidden addresses, values, and types).
- Received data is saved with its received order and byte values preserved (MD data is not swapped or interleaved; PCE bit reversal is decided by DB matching).

### DB

- Format validation (version, required fields, consistency of header and size, duplicate IDs, and conflicting records).
- Candidate search by partial hash, the conditions for the automatic-decision group, and the reasons for unsupported mappers.
- The counts, metadata, and support conditions of the bundled DBs are verified. A tool for regenerating the DBs is not included in this source.

### Saving

- Publishing through a temporary file, never replacing an existing file, reuse of a file with the same name and content, saving under an alternate name when the name is the same and the content differs, and cleanup of old temporary files.
- The Linux system-call paths (renameat2, the direct call on ARM EABI, and the hard-link fallback) are verified with simulated calls.
- Re-checking the file after saving, and verifying an alternate-named file by the hash of its content rather than by its name.

### Settings and environment

- Validation of the save-folder and core settings (rejecting invalid values, filling in defaults, and priority), per-system save folders, and automatic environment detection.
- The content and file name of the MGL, and the launch command to the FIFO (when it is not a FIFO, when nobody is listening, and when it is too long).
- Names and launchers.

### Screens

- Windows GUI: connection, automatic decision, candidate selection, declining with No, a name conflict, preventing re-operation while busy, cancellation, closing, and keeping keyboard focus.
- MiSTer manager screen: the order of the menu and the initial cursor, the candidate screen, the confirmation screen (default No), progress, truncation of the display by terminal size, and decoding of gamepad input.

### Distribution

- That the inclusion patterns select only the intended files (excluding personal settings, logs, ROMs, and native binaries).
- That the manifest's SHA-256 matches the ZIP's contents.

### Environment differences

- MiSTer's minimal Python configuration may lack the Shift-JIS codec. Confirm that the tests also pass in a simulated environment without the codec.
- That the code passes as Python 3.9 syntax.
