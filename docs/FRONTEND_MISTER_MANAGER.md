# MiSTer manager screen design

[日本語](ja/FRONTEND_MISTER_MANAGER.md)

## Role

- An English text menu for a TV and a gamepad, started from MiSTer's Scripts menu. It draws on a Linux terminal (fb_terminal) and depends on neither an image library nor Japanese fonts.
- Operation relies on the mechanism by which MiSTer's standard, already configured gamepad is converted into keys such as up, down, Enter, and Esc. It has no driver for individual gamepads.
- Listing and selecting saved games is left to ConsoleMode. The manager screen only imports, and launches the title that was imported last.
- It does not start if the terminal is not interactive (not a tty). If run from SSH, the screen appears on the SSH terminal, not on the TV.
- On a PC, `manager --preview` lets you check the screen alone without opening the reader.

## Screens

### Main screen

```text
==============================================================================
 Cartridge Station for RetroFreak / MiSTer FPGA Edition
 Cart: <cartridge state>
 Game: <title>
 <details>                                (when the height is 21 lines or more)
 Reader: <reader state>
 ROM: Verified file available / Not saved / not verified
 Core: Ready / Core/settings not available
 > CHECK / RETRY
   SELECT CARTRIDGE
   DUMP CARTRIDGE
   GAME START
   EXIT TO MiSTer
 <message, progress bar, or confirmation>
 D-pad: Move   OK: Select   Back: Cancel
==============================================================================
```

- The display is 40–100 columns wide and at least 16 lines high. On a small terminal the details line is omitted. The last line is not used, to prevent scrolling.
- Only printable ASCII is shown. Anything else is replaced with a space or `?`.
- Items that cannot be run are marked `[unavailable]`.

### Menu items and when they are enabled

| Item | Action | Enabled when |
|---|---|---|
| CHECK / RETRY | Check the reader and cartridge again (closes the connection and reconnects) | Not busy |
| SELECT CARTRIDGE | Open the list of candidates | There are candidates and not busy |
| DUMP CARTRIDGE | Import after a confirmation | A readable candidate is selected, or there is an automatic-decision group, and not busy |
| GAME START | Start the game with the verified ROM after a confirmation | A verified saved ROM exists, the core is available, and not busy |
| EXIT TO MiSTer | Exit the manager screen | Not busy |

### Candidate list

- Shows the titles, a READER SUPPORTED or UNSUPPORTED label, and, for the selected candidate, its details and the reason it is or is not supported.
- Guidance:
  - READER SUPPORTED means "the reader's read code supports it". It is not a guarantee that the title and revision match the real cartridge.
  - A partial hash can match other titles and revisions as well.
  - The title and revision are not confirmed until the full-ROM hash matches.
  - The ROM is saved only if it matches the full-ROM hash of the chosen title.
- For an automatic-decision group, the guidance is "Optional choice; DUMP can identify these titles automatically."
- Even when several candidates are detected, the main screen is shown first. The list opens only when SELECT CARTRIDGE is chosen.

### Confirmation screen

- Shown for DUMP, GAME START, and EXIT. The choices are YES and NO, and the default is NO. Up/left selects YES, down/right selects NO. OK confirms. Back cancels.
- Confirmation text: for DUMP, "Start reading the selected cartridge?" (for an automatic decision, "Read the cartridge and pick the title by hashes?"); for GAME START, "Start the verified game?"; for EXIT, "Exit to MiSTer?".
- Back during a read requests a stop at a safe boundary (it shows "Stopping safely..."). If it stops before saving, no new ROM is saved. If a save write has already begun, finishing it takes priority.

### Progress

- Shows the stage name, a 24-column progress bar, and the percentage.

## Initial cursor and transitions

| State after automatic checking | Initial cursor | SELECT CARTRIDGE |
|---|---|---|
| Several candidates (including a group that can be decided automatically) | SELECT CARTRIDGE | Shown and enabled |
| One candidate, readable | DUMP CARTRIDGE | Shown and enabled (only one entry when opened) |
| One candidate, unsupported | CHECK / RETRY | Shown and enabled (the reason it is unsupported can be read) |
| Empty or no candidates | CHECK / RETRY | Shown and disabled |

- The cursor advances only when candidates first appear. It does not move on every poll.
- After a candidate is selected, the cursor goes to DUMP if it is supported, and stays on SELECT if it is not.
- After an import and verification complete, it moves on to GAME START.
- If the set of candidates changes, the screen returns to the main screen and the selection is discarded.

## Input

- Interprets the arrow-key escape sequences (`ESC [ A` and so on), `w a s d`, Tab (down), Enter/Space (OK), and `q`/Backspace/a lone Esc (Back).
- A lone Esc is treated as "Back" if no following character arrives within 120 ms.
- Only the first action of one input batch is handled, and the input right after it is discarded along with the input buffer (0.15 seconds). Input is ignored for 0.3 seconds right after a process completes (to prevent mistaken operation from button mashing).

## Processing structure

- The screen loop runs on one thread. Processing that touches the reader (poll, retry, select, capture, prepare_game) is requested of one worker, one at a time.
- A state check (poll) is requested on the main screen, not while a confirmation is shown, and when 0.5–0.7 seconds have passed since the last one.
- While busy, only the cursor can be moved with up and down.
- Progress arrives from the worker through a queue. The progress callback checks for a stop request and raises an exception.

### How results are reflected

- poll / select: reflects the state in the menu and checks again whether the core for that system is available. After a select, the cursor goes to DUMP if the candidate is supported, and to SELECT otherwise.
- capture: sets "saved" to true and puts the cursor on GAME START. For a new save it shows "Saved and verified. Select GAME START."; for an existing identical file, "Already saved; identical file verified."; for a save under an alternate name, "Saved with hash suffix; existing file preserved. GAME START ready."
- launch: restores the terminal, sends the launch request using the MGL, and exits. If sending the request fails, it returns to the terminal and shows the error.

### Errors

- When an exception occurs, the message is shown (rounded to ASCII), the candidates and the selection are discarded, the cursor returns to the top, and recovery guidance such as "Check cartridge/USB connection; select CHECK / RETRY." is shown. A failure to launch a game shows "Check game/core settings; select CHECK / RETRY."
- Details are appended to `runtime/manager.log` with the time, exception name, and content. If writing to the log fails, processing continues.
- When the log reaches 1 MiB it is moved to `manager.log.1` (replacing any earlier `.1`) and a new log is started, so that it does not grow without limit on the SD card.
- After a communication error the tool does not reconnect automatically. CHECK / RETRY reconnects. Only after a cancellation does automatic polling resume.

## Preventing a double launch

- It takes an exclusive (non-blocking) lock on a lock file. If it cannot, it does not start, treating another instance as running.
- Do not run another command that uses the same reader at the same time.

## Launching a game and returning

- GAME START releases the reader, re-verifies the saved ROM of the selected title against the header, PRG, CHR, and the whole, and then starts the matching core with the ROM specified in an MGL. The manager screen exits.
- The MGL for launching is generated in the app's `runtime/`.
- Returning from the game to ConsoleMode is done with a MiSTer operation (Reboot). The manager screen has no resident feature that detects the end of the game and shows itself again.
- To reflect a saved ROM in ConsoleMode's list, rebuild ConsoleMode's database. Launching directly with GAME START does not require rebuilding the list database.
- Changing the selection also changes which saved ROM is matched. The previously selected game is never launched in its place.
- Merely removing a cartridge (empty, several inserted, or an unknown state) keeps the selection, and the saved game can still be launched with GAME START. When a readable cartridge is newly inserted, the selection is cleared. When a cartridge with the same header is reinserted, the DB-verified title is carried over.

## Save folder

- At startup the save folder is the NES save folder in the settings. After that it switches to the save folder of each detected system. If `--output-dir` is given, it is fixed.
- If the destination already has a file with the same name and different content, it is not overwritten; the ROM is saved under an alternate name.

## Tests

- The drawing of the screen, decoding of key input, the menu's enabled conditions and order, the initial cursor, the candidate list, confirmation, progress, recovery from errors, and truncation by terminal size are verified with a simulated service and a simulated terminal.
