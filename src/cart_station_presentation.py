"""Profile presentation shared by the Windows and MiSTer frontends."""


def _kib(profile):
    return '%d KiB' % (profile.prg_size // 1024)


def _auto_kib(profile):
    return _kib(profile) if profile.prg_size else 'Auto (DB)'


def _sfc(profile):
    state = 'DB verified' if profile.raw_sha1 else 'Header detected; full DB verification after reading'
    return 'SFC / %s / %s / ROM %s / %s' % (profile.controller, profile.chip, _kib(profile), state)


def _pce(profile):
    state = ('DB verified / ' + profile.data_order if profile.raw_sha1
             else 'Sample detected; size/title verified after reading')
    return '%s / %s / ROM %s / %s' % (profile.system, profile.controller, _auto_kib(profile), state)


def _md(profile):
    state = 'DB verified' if profile.raw_sha1 else 'Header range; full DB verification after reading'
    return 'MD / %s / %s / ROM %s / %s' % (profile.serial, profile.controller, _kib(profile), state)


def _gba(profile):
    state = 'DB verified' if profile.raw_sha1 else 'Header detected; size/title verified after reading'
    return 'GBA / %s / ROM %s / %s' % (profile.game_code, _auto_kib(profile), state)


def _gb(profile):
    state = 'DB verified' if profile.raw_sha1 else 'Header detected; full DB verification after reading'
    return '%s / %s / ROM %s / %s' % (profile.system, profile.controller, _kib(profile), state)


# system -> (details line, table columns); anything else is an NES profile.
_SYSTEMS = {
    'SFC': (_sfc, lambda p: (p.controller, _kib(p), 'SFC')),
    'PCE': (_pce, lambda p: (p.controller, _auto_kib(p), p.system)),
    'SGX': (_pce, lambda p: (p.controller, _auto_kib(p), p.system)),
    'MD': (_md, lambda p: (p.controller, _kib(p), 'MD')),
    'GBA': (_gba, lambda p: (p.controller, _auto_kib(p), 'GBA')),
    'GB': (_gb, lambda p: (p.controller, _kib(p), p.system)),
    'GBC': (_gb, lambda p: (p.controller, _kib(p), p.system)),
}


def profile_details(profile):
    entry = _SYSTEMS.get(getattr(profile, 'system', 'NES'))
    if entry:
        return entry[0](profile)
    return 'Mapper %d.%d / PRG %d KiB / CHR %d KiB' % (
        profile.mapper, profile.submapper, profile.prg_size // 1024, profile.chr_size // 1024)


def profile_columns(profile):
    entry = _SYSTEMS.get(getattr(profile, 'system', 'NES'))
    if entry:
        return entry[1](profile)
    return ('%d.%d' % (profile.mapper, profile.submapper),
            '%d KiB' % (profile.prg_size // 1024), '%d KiB' % (profile.chr_size // 1024))
