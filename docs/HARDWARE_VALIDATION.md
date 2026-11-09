# Managing hardware validation

[日本語](ja/HARDWARE_VALIDATION.md)

Software tests, imports of real cartridges, and the in-game behavior of saved ROMs are recorded separately. A match with a synthetic bus or with reference traces alone is not called "verified on hardware". The real reader was not operated during the latest restructuring, so the verified range on hardware has not grown.

## Priorities and open scope

| Priority | Target | Current evidence and what remains to be confirmed |
|---|---|---|
| 1 | SFC SA-1 and S-DD1 | Reconstruction of up to 8 MiB and the ROM-mapping writes are checked on a synthetic bus. Success on a real cartridge is unconfirmed. Prioritize the first-half/second-half boundary and the re-initialization before the second read |
| 1 | SFC CX4 | Limited to one permitted title, LoROM, up to 2 MiB. Only synthetic tests so far; success on hardware is unconfirmed. Verify the current real-header check, 0x7F52=0, and the order of the re-check |
| 2 | NES boards by mapper and submapper | See SYSTEM_FC_NES.md for the support conditions and the explicit unverified range. Prioritize dedicated low-address operations, SRAM protection, bus conflicts, and boards with wiring differences. Do not reuse the success of a different board |
| 3 | Each system, transport, and host | Keep synthetic tests of communication and observations of state replies separate from successful imports of whole ROMs. Record the results of serial vs. direct USB and Windows vs. MiSTer individually |

The number of DB entries and the read conditions are not counts of successes on hardware. A target with no individual success record here cannot be judged "verified on hardware" from this document. Methods that the implementation refuses, such as SPC7110, are not unlocked for the sake of validation.

## What one validation records

1. Date, source commit, host and OS, transport, and the reader's build/product.
2. The cartridge's title, region, and revision; the board information that can be confirmed; and the DB profile ID. For NES, also the mapper/submapper; for SFC, also the method, chip, and capacity.
3. The command and settings used, and the result from start to finish. If there is an I/O record, attach where it is stored and its SHA-256.
4. Whether the two independent reads matched, whether the full-ROM hash in the DB matched, and whether the re-check after saving passed. Also check the capacity boundary and the last bank. A mismatch, interruption, or disconnection is not counted as a success.
5. Record the in-game behavior as a separate item. If it was not run, write "not run"; if it was, record the core and version used and the range checked.

Record template:

```text
Date / source commit:
Host / OS / transport:
Reader build / product:
Title / region / revision / board:
DB profile ID / method / capacity:
Command / settings:
Two reads match / DB match / re-check after saving:
Boundary and last-bank check:
Location of the communication record / SHA-256:
In-game behavior (core and version used and range checked, or "not run"):
Failures / unconfirmed items:
```

When reporting a success, state the range of boards, hosts, and transports that the record ties it to.
