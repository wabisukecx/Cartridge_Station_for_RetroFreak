# Windows GUI design

[日本語](ja/FRONTEND_WINDOWS_GUI.md)

## Role

- A PyQt6 screen for Windows only. The MiSTer edition has a separate screen (a text menu) that does not need PyQt6.
- Reading, DB decisions, two reads, hash matching, and protection of same-named files use the shared service layer; the GUI has only screen control and display.
- The screen text is in English. Apart from opening the ROM save folder, it has no emulator launch and no FTP transfer. It also has no MiSTer core-launch operation.
- Start it with the command `cart_station.py gui` or with the Windows launcher. At startup it only lists the ports; it does not connect to the reader or read anything. It connects only when Connect is pressed.

## Threads

- Two threads: the GUI thread and a worker thread dedicated to the reader (QThread). All communication over USB or serial and all file verification happen on the worker.
- The GUI asks the worker for an "operation". Only one operation runs at a time (other operations are not accepted while one is running).

| Operation | Meaning |
|---|---|
| connect | Create the service and do the first poll |
| poll | Periodic check of the cartridge state (every 600 ms; not sent during an import, while a confirmation dialog is shown, or during shutdown) |
| select | Select a candidate |
| capture | Import. On success it returns the saved path, whether it was newly created or an existing file was reused, and the latest state |
| disconnect | Close the service |

- Results arrive through three signals: success, failure, and progress.
- On failure, the service is closed and the screen returns to the disconnected state. It shows "Reader session closed — check cartridge/USB connection, then click Connect.", and the user presses Connect to reconnect. On cancellation it shows "Stopped safely — click Connect to continue."
- The progress callback checks for a cancel request and raises a cancellation exception at a safe boundary.

## Screen layout

| Area | Contents |
|---|---|
| Title | Product name, Windows Edition, and a summary of supported systems. A note that NES needs a converter |
| Reader & destination | Connection (port choice, Refresh ports, Connect/Disconnect), save folder (Choose folder…), and DB (Custom DB…, Use default DB) |
| Status | The state text (the cartridge state and the stage of progress) |
| Title / details | The title and details (mapper, capacity, and so on) of the selected candidate |
| Candidate table | Title, Mapper, PRG, CHR, Support (for GB-family: Controller, ROM, System, Support). Whole rows are selected. Used to choose when there are several candidates |
| Progress bar | 0–100% |
| Buttons | Read cartridge…, Cancel reading, Open save folder |
| Result | The save result and guidance |
| Activity | A log of up to the most recent 200 lines |

- While connected, changing the port, save folder, and DB is disabled (Disconnect to change them). The connection method is Auto-detect or a specified COM port. Direct USB is not shown.
- All labels are treated as plain text (so that strings from outside are not interpreted as rich text).

## Flow of operation

1. Choose the connection method, choose a save folder and your own DB if needed, and press Connect.
2. After connecting, the tool checks the cartridge periodically, identifies it, and shows the candidates.
3. Several candidates that can be read by the same procedure are decided automatically by full-ROM hash ("Auto-detect among N candidates"). If a manual choice is needed, pick the title you inserted from the table. An unsupported candidate shows the reason and disables Read cartridge.
4. Pressing Read cartridge shows a confirmation dialog (default No) with the title and the save folder. Only on Yes do the two independent reads for saving begin. PCE also reads the whole ROM once during automatic identification and matches it against the DB, but it does not save at that stage. For other systems, identification reads the header or partial hashes.
5. Progress is shown, and the ROM is saved after two independent reads and a full-ROM hash match. A file with the same name and content is reused. For the same name with different content, the original file is kept and the ROM is saved under a hash-qualified name (for an alternate name, the screen says the existing file was left in place).
6. Open save folder opens the save folder. If it does not exist, the screen says it will be created when the first ROM is saved.

### Cancellation and exit

- Cancel reading requests a stop at a safe boundary. If a save write has already begun, it waits for it to finish.
- Identification (poll and connect) can also be stopped.
- Trying to close the window during a read asks for confirmation; on Yes it stops at a safe boundary and closes. It waits for the worker to finish before closing the window.
- If Disconnect is pressed during a state check, the tool disconnects after the current check ends.

### Keeping the candidate selection

- A state update rebuilds the candidate table only when the set of candidates changes. The selected row is kept and keyboard focus is not lost.
- If the user changes the selection during a state check, it is held back and applied when the worker is free.

## Save folder

- The default save folder is the per-system save folder in the settings. Each time a system is detected, it switches to that system's folder.
- A save folder chosen with Choose folder, or specified with `--output-dir` at startup, is fixed and is not switched by system.
- Import measurements are recorded in `runtime/last_capture_metrics.json`.

## DB

- The standard DBs are loaded automatically. They are bundled as JSON, not embedded in an executable.
- If a personal DB (config/nes_profiles.json) exists, it is used. An external DB chosen with Custom DB is effective only for FC. GB/GBC, GBA, MD, PCE, and SFC use the bundled DBs.
- Search is used only to identify candidates for the inserted cartridge. There is no screen for searching the whole DB by hand.

## Installation

- Python 3.9 or later. In the extracted folder, create a virtual environment, install `requirements-windows.txt`, and start it.
- Creating the virtual environment and installing the dependencies is done in the user's environment. No exe is built.

## Tests

- With a simulated cartridge and Qt's offscreen mode, the tests verify connection, candidate selection, declining with No, a name conflict, preventing re-operation while busy, cancellation, closing, and keeping the selection and focus. They do not open a real reader.
