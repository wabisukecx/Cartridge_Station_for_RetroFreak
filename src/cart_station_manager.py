"""MiSTer TV terminal UI; standard gamepad mapping supplied by Main_MiSTer.

Shared ROM and transport code stays in the service and system modules. No framebuffer library required.
"""
import os
import queue
import select
import shutil
import sys
from cart_station_branding import PRODUCT_NAME, MISTER_EDITION, MISTER_LOCK_PATH
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from cart_station_protocol import ProtocolError
from cart_station_environment import default_output_directory
from cart_station_paths import ROOT
from cart_station_nes_db import Database
from cart_station_presentation import profile_details
from cart_station_romaji import romanize

from cart_station_service import (Service, PollState, UserCancelled, verified_file,
                                 read_core_config, core_available, create_mgl, send_launch)

MENU_CHECK, MENU_SELECT, MENU_DUMP, MENU_START, MENU_EXIT = range(5)
MENU_LABELS = ('CHECK / RETRY', 'SELECT CARTRIDGE', 'DUMP CARTRIDGE',
               'GAME START', 'EXIT TO MiSTer')


def safe_text(value, width=72):
    value = romanize(value)  # kana titles from Japanese headers; the console is ASCII-only
    return ''.join(char if 32 <= ord(char) <= 126 else ' ' if ord(char) < 32 else '?' for char in value)[:width]


@dataclass
class Screen:
    reader: str = 'Connecting...'
    cartridge: str = 'Checking cartridge...'
    supported: bool = False
    saved: bool = False
    selected: int = 0
    busy: bool = False
    stage: str = ''
    percent: int = 0
    message: str = 'Insert a cartridge. Choose a DB candidate, then confirm.'
    recovery_hint: str = ''
    confirm: str = ''
    confirm_yes: bool = False
    error: bool = False
    core_ready: bool = False
    profile: object = None
    candidates: tuple = ()
    view: str = 'main'
    choice: int = 0
    selection_pending: bool = False
    confirm_action: str = ''
    auto: int = 0  # number of same-geometry candidates read together
    def enabled(self):
        return [not self.busy, bool(self.candidates) and not self.busy,
                (self.supported or bool(self.auto)) and not self.busy,
                self.saved and self.core_ready and not self.busy,
                not self.busy]

    def apply(self, state):
        old_candidates = self.candidates
        changed = (self.profile != state.profile or self.supported != state.supported
                   or self.cartridge != state.cartridge or old_candidates != state.candidates)
        self.reader, self.cartridge, self.supported = state.reader, state.cartridge, state.supported
        self.profile, self.candidates, self.saved = state.profile, state.candidates, state.saved
        self.selection_pending = state.selection_pending
        old_auto, self.auto = self.auto, state.auto
        if self.candidates != old_candidates:
            self.choice = 0
            # Detection always returns to the main screen. Open the picker
            # only through SELECT CARTRIDGE, for both manual and auto groups.
            self.view = 'main'
            if not self.candidates:
                self.selected = MENU_CHECK
            elif not old_candidates:
                # Advance once after detection/retry, not on every poll or
                # when a completed dump reduces the candidate list to one.
                self.selected = (MENU_SELECT if len(self.candidates) > 1 else
                                 MENU_DUMP if self.supported else MENU_CHECK)
        if not self.error and state.cartridge == 'FC/NES: no DB candidates':
            self.message = 'No DB match. Check cartridge contacts, then CHECK / RETRY.'
        elif not self.error and (changed or old_auto != self.auto) and self.auto:
            self.message = ('%d titles match. DUMP reads the cartridge and picks the title by full hashes.'
                            % self.auto)
        elif not self.error and changed:
            if state.selection_pending:
                self.message = 'Open SELECT CARTRIDGE and choose the cartridge you inserted.'
            elif self.profile and not self.supported and self.candidates:
                self.message = self.profile.support() or 'Check cartridge before dumping.'
            elif self.supported:
                self.message = ('DB candidate selected. Complete hashes are checked after reading.'
                                if getattr(self.profile, 'mapper', None) == 206 else
                                'Software-tested reader. Complete ROM hashes will be checked.')


def render(screen, destination, width=78, height=25):
    width = max(40, min(width, 100))
    height = max(16, height)
    border = '=' * (width - 2)

    def line(text, selected=False):
        text = safe_text(text, width - 2)
        return '\x1b[7m' + text.ljust(width - 2) + '\x1b[0m' if selected else text

    if screen.view == 'candidates':
        lines = [border, ' SELECT CARTRIDGE / %d DB candidate(s)' % len(screen.candidates)]
        count = max(2, height - 12)
        first = screen.choice // count * count
        for index in range(first, min(len(screen.candidates), first + count)):
            profile = screen.candidates[index]
            badge = ' [READER SUPPORTED]' if profile.support() is None else ' [UNSUPPORTED]'
            label = (' > ' if index == screen.choice else '   ') + profile.title
            lines.append(line(safe_text(label, width - 2 - len(badge)) + badge, index == screen.choice))
        if screen.candidates:
            profile = screen.candidates[min(screen.choice, len(screen.candidates) - 1)]
            lines += [line(' ' + profile_details(profile)),
                      line(' ' + (profile.support() or 'Reader supported; title/revision match is not guaranteed.'))]
        partial = any(getattr(p, 'mapper', None) is not None for p in screen.candidates)
        verified_choice = bool(screen.saved and screen.profile and screen.candidates
                               and profile.id == screen.profile.id)
        hint = ('Optional choice; DUMP can identify these titles automatically.' if screen.auto
                else 'Saved ROM verified; re-reading checks hashes again.' if verified_choice
                else 'Title/revision unconfirmed until full ROM hashes match.')
        save_hint = ('Saved only after a full ROM hash match.' if screen.auto
                     else 'Saved only if full ROM hashes match the selected title.')
        lines += [line(' Partial hashes can match different titles/revisions.' if partial else
                       ' A candidate title alone does not verify the ROM.'),
                  line(' ' + hint), line(' ' + save_hint),
                  line(' D-pad: Move   OK: Choose   Back: Cancel'), border]
        return '\r\n'.join(lines)

    profile = screen.profile
    title = profile.title if profile else 'No game selected'
    if screen.auto:
        profile, title = None, 'Auto-detect among %d candidates' % screen.auto
    elif screen.selection_pending:
        profile, title = None, 'Select the cartridge you inserted'
    details = (profile_details(profile) if profile else
               'Title is decided after reading' if screen.auto else
               'Choose SELECT CARTRIDGE before DUMP.' if screen.selection_pending else
               'Waiting for a DB match')
    lines = [border, line(' ' + PRODUCT_NAME + ' / ' + MISTER_EDITION),
             line(' Cart: ' + screen.cartridge), line(' Game: ' + title)]
    if height >= 21:
        lines += [line(' ' + details), line(' Reader: ' + screen.reader),
                  line(' ROM: ' + ('Verified file available' if screen.saved else 'Not saved / not verified')),
                  line(' Core: ' + ('Ready' if screen.core_ready else 'Core/settings not available'))]
    for index, label in enumerate(MENU_LABELS):
        text = (' > ' if index == screen.selected else '   ') + label
        if not screen.enabled()[index]:
            text += '  [unavailable]'
        lines.append(line(text, index == screen.selected))
    if screen.busy:
        percent = max(0, min(100, screen.percent))
        fill = percent * 24 // 100
        lines += [line(' ' + screen.stage),
                  ' [' + '#' * fill + '-' * (24 - fill) + '] %3d%%' % percent]
    elif screen.confirm:
        lines += [line(' ' + screen.confirm),
                  '   ' + ('[YES]   No' if screen.confirm_yes else ' Yes   [NO]')]
    else:
        lines += [line(' ' + ('ERROR: ' if screen.error else '') + screen.message),
                  line(' ' + screen.recovery_hint) if screen.recovery_hint else '']
    lines += [line(' D-pad: Move   OK: Select   Back: Cancel'), border]
    return '\r\n'.join(lines)


class KeyDecoder:
    def __init__(self):
        self.buffer = b''
        self.escape_at = 0.0

    def feed(self, data, now):
        self.buffer += data
        actions = []
        sequences = {b'\x1b[A': 'up', b'\x1bOA': 'up', b'\x1b[B': 'down', b'\x1bOB': 'down',
                     b'\x1b[C': 'right', b'\x1bOC': 'right', b'\x1b[D': 'left', b'\x1bOD': 'left'}
        while self.buffer:
            if self.buffer.startswith(b'\x1b'):
                matched = next((item for item in sequences if self.buffer.startswith(item)), None)
                if matched:
                    actions.append(sequences[matched])
                    self.buffer = self.buffer[len(matched):]
                    self.escape_at = 0
                    continue
                if any(item.startswith(self.buffer) for item in sequences):
                    if not self.escape_at:
                        self.escape_at = now
                    if now - self.escape_at < 0.12:
                        break
                actions.append('back')
                self.buffer = self.buffer[1:]
                self.escape_at = 0
                continue
            key, self.buffer = self.buffer[:1], self.buffer[1:]
            action = {b'\r': 'select', b'\n': 'select', b' ': 'select', b'\t': 'down',
                      b'w': 'up', b's': 'down', b'a': 'left', b'd': 'right',
                      b'q': 'back', b'\x7f': 'back', b'\x08': 'back'}.get(key)
            if action:
                actions.append(action)
        return actions


class Terminal:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.original = None
        self.decoder = KeyDecoder()

    def resume(self):
        import termios
        import tty
        if self.original is None:
            self.original = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self.discard()
        sys.stdout.write('\x1b[?25l')
        sys.stdout.flush()

    def pause(self):
        import termios
        if self.original is not None:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.original)
            self.discard()
        sys.stdout.write('\x1b[0m\x1b[?25h\r\n')
        sys.stdout.flush()

    def discard(self):
        import termios
        termios.tcflush(self.fd, termios.TCIFLUSH)
        self.decoder = KeyDecoder()

    def read(self, timeout=0.1):
        data = b''
        if select.select([self.fd], [], [], timeout)[0]:
            data = os.read(self.fd, 64)
            if not data:
                raise EOFError('TV terminal closed')
        return self.decoder.feed(data, time.monotonic())

    def draw(self, text):
        rows = shutil.get_terminal_size((80, 25)).lines
        # Leave the final terminal row unused to avoid scrolling the menu.
        sys.stdout.write('\x1b[H\x1b[2J' + '\r\n'.join(text.split('\r\n')[:max(10, rows - 1)]))
        sys.stdout.flush()


LOG_LIMIT_BYTES = 1024 * 1024


def log_error(error, path=None):
    try:
        path = path or ROOT / 'runtime' / 'manager.log'
        path.parent.mkdir(exist_ok=True)
        # Keep the SD card bounded: one previous generation, then start anew.
        if path.is_file() and path.stat().st_size >= LOG_LIMIT_BYTES:
            os.replace(str(path), str(path.with_name(path.name + '.1')))
        with path.open('a', encoding='utf-8') as stream:
            stream.write('%s %s: %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), type(error).__name__, error))
    except OSError:
        pass


def run_manager(args, environment):
    destination = args.output_dir or default_output_directory(environment, 'NES', args.paths_config)
    screen = Screen()
    if args.preview:
        database = Database.load(getattr(args, 'db', None))
        screen.profile = next((p for p in database.profiles if p.support() is None), None)
        screen.candidates = (screen.profile,) if screen.profile is not None else ()
        screen.reader = 'Connected (preview)'
        screen.cartridge = 'FC/NES: DB candidate selected'
        screen.supported = screen.saved = screen.core_ready = True
        screen.message = 'Ready. Select DUMP CARTRIDGE or GAME START.'
        print(render(screen, destination))
        return
    if environment != 'mister':
        raise ProtocolError('The TV manager runs on MiSTer. Use --preview for a PC preview.')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ProtocolError('Open ' + PRODUCT_NAME + ' from MiSTer Scripts with fb_terminal enabled')
    import fcntl
    import threading
    with open(MISTER_LOCK_PATH, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ProtocolError('Another ' + PRODUCT_NAME + ' instance is already running') from error
        service = Service(destination, args.port, args.usb, args.core_config,
                          db_path=getattr(args, 'db', None), environment=environment,
                          paths_config=args.paths_config, follow_system_paths=args.output_dir is None,
                          metrics_path=ROOT / 'runtime/last_capture_metrics.json')
        terminal = Terminal()
        cancelled = threading.Event()
        updates = queue.Queue()
        executor = ThreadPoolExecutor(max_workers=1)
        future = None
        task = ''
        next_poll = 0.0
        poll_enabled = True
        previous = ''
        ignore_until = 0.0

        def progress(stage, done, total):
            if cancelled.is_set():
                raise UserCancelled('Cancelled at a safe boundary; no new ROM saved')
            updates.put((stage, int(done * 100 / total) if total else 0))

        service.identification_progress = progress

        try:
            screen.profile = service.profile
            screen.saved = service.existing() is not None
            try:
                screen.core_ready = core_available(read_core_config(args.core_config), service.sd_root)
            except ProtocolError as error:
                screen.message = safe_text(error)
            terminal.resume()
            while True:
                now = time.monotonic()
                while not updates.empty():
                    screen.stage, screen.percent = updates.get_nowait()
                    if task == 'poll':
                        screen.busy = True
                if future is not None and future.done():
                    try:
                        result = future.result()
                        screen.recovery_hint = ''
                        if task in ('poll', 'select'):
                            screen.apply(result)
                            if task == 'select':
                                screen.selected = MENU_DUMP if screen.supported else MENU_SELECT
                            try:
                                system = getattr(service.profile, 'system', 'NES')
                                screen.core_ready = core_available(read_core_config(args.core_config, system), service.sd_root)
                            except ProtocolError:
                                screen.core_ready = False
                        elif task == 'capture':
                            screen.profile = service.profile
                            screen.auto = 0
                            screen.saved = True
                            screen.selected = MENU_START
                            screen.message = ('Saved and verified. Select GAME START.' if result[1]
                                              else 'Already saved; identical file verified.')
                            if '.verified-' in result[0].name:
                                screen.message = 'Saved with hash suffix; existing file preserved. GAME START ready.'
                            screen.error = False
                        elif task == 'launch':
                            if cancelled.is_set():
                                raise UserCancelled('Game start cancelled')
                            terminal.pause()
                            try:
                                send_launch(result)
                            except Exception:
                                terminal.resume()
                                raise
                            return
                    except Exception as error:
                        if not isinstance(error, UserCancelled):
                            log_error(error)
                        screen.message = safe_text(error)
                        screen.error = not isinstance(error, UserCancelled)
                        screen.supported = False
                        screen.candidates, screen.view = (), 'main'
                        screen.selection_pending = False
                        screen.auto = 0
                        screen.selected = 0
                        screen.reader = ('Not connected / select CHECK to retry'
                                         if service.adapter is None else service.product)
                        screen.recovery_hint = (
                            'Select CHECK / RETRY to read again.' if isinstance(error, UserCancelled) else
                            'Check game/core settings; select CHECK / RETRY.' if task == 'launch' else
                            'Check cartridge/USB connection; select CHECK / RETRY.'
                        )
                        screen.profile = service.profile
                        screen.saved = service.existing() is not None
                        poll_enabled = isinstance(error, UserCancelled) and task != 'poll'
                    completed_task = task
                    future = None
                    screen.busy = False
                    if completed_task != 'poll':
                        terminal.discard()
                        ignore_until = now + 0.3
                    next_poll = now + 0.7
                if (future is None and poll_enabled and not screen.confirm
                        and screen.view == 'main' and now >= next_poll):
                    cancelled.clear()
                    future, task = executor.submit(service.poll), 'poll'
                    next_poll = now + 0.5
                size = shutil.get_terminal_size((80, 25))
                picture = render(screen, service.destination, size.columns, size.lines)
                if picture != previous:
                    terminal.draw(picture)
                    previous = picture
                actions = terminal.read()
                for action in actions[:1]:
                    if now < ignore_until:
                        continue
                    if screen.busy:
                        if action == 'back':
                            cancelled.set()
                            screen.stage = 'Stopping safely...'
                        continue
                    if future is not None:
                        if task == 'poll' and screen.view == 'main' and action in ('up', 'down'):
                            screen.selected = (screen.selected + (1 if action == 'down' else -1)) % 5
                        continue
                    if screen.view == 'candidates':
                        if action in ('up', 'down') and screen.candidates:
                            screen.choice = (screen.choice + (1 if action == 'down' else -1)) % len(screen.candidates)
                        elif action == 'back':
                            screen.view = 'main'
                        elif action == 'select' and screen.candidates:
                            identifier = screen.candidates[screen.choice].id
                            screen.view = 'main'
                            screen.busy, screen.stage = True, 'Selecting DB candidate...'
                            future, task = executor.submit(service.select_profile, identifier), 'select'
                    elif screen.confirm:
                        if action in ('up', 'left'):
                            screen.confirm_yes = True
                        elif action in ('down', 'right'):
                            screen.confirm_yes = False
                        elif action == 'back' or (action == 'select' and not screen.confirm_yes):
                            screen.confirm, screen.confirm_action = '', ''
                        elif action == 'select':
                            chosen = screen.confirm_action
                            screen.confirm, screen.confirm_action = '', ''
                            cancelled.clear()
                            if chosen == 'exit':
                                return
                            screen.busy, screen.percent = True, 0
                            if chosen == 'capture':
                                screen.stage = 'Preparing selected cartridge...'
                                future, task = executor.submit(service.capture, progress), 'capture'
                            elif chosen == 'launch':
                                screen.stage = 'Verifying selected ROM and preparing NES...'
                                future, task = executor.submit(service.prepare_game), 'launch'
                    elif action in ('up', 'down'):
                        screen.selected = (screen.selected + (1 if action == 'down' else -1)) % 5
                    elif action == 'back':
                        screen.confirm, screen.confirm_yes = 'Exit to MiSTer?', False
                        screen.confirm_action = 'exit'
                    elif action == 'select' and screen.enabled()[screen.selected]:
                        if screen.selected == MENU_CHECK:
                            screen.supported, screen.error = False, False
                            screen.candidates = ()
                            screen.recovery_hint = ''
                            screen.reader, screen.message = 'Connecting...', 'Checking reader...'
                            poll_enabled, next_poll = True, 0.0
                            future, task = executor.submit(service.retry), 'poll'
                        elif screen.selected == MENU_SELECT:
                            screen.view = 'candidates'
                            screen.choice = 0
                        else:
                            screen.confirm_yes = False
                            screen.confirm = {MENU_DUMP: ('Read the cartridge and pick the title by hashes?'
                                                  if screen.auto else 'Start reading the selected cartridge?'),
                                              MENU_START: 'Start the verified game?', MENU_EXIT: 'Exit to MiSTer?'}[screen.selected]
                            screen.confirm_action = {MENU_DUMP: 'capture', MENU_START: 'launch', MENU_EXIT: 'exit'}[screen.selected]
                    terminal.discard()
                    ignore_until = now + 0.15
        except (KeyboardInterrupt, EOFError):
            cancelled.set()
        finally:
            cancelled.set()
            executor.shutdown(wait=True)
            try:
                service.close()
            except Exception as error:
                log_error(error)
            terminal.pause()
