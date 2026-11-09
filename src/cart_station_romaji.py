"""Kana to ASCII romaji for the ASCII-only MiSTer terminal.

Header titles of Japanese cartridges are often half-width katakana. The TV
console cannot draw them, so the preview shows romaji instead. Display only:
identity and DB matching never use this text. Pure Python on purpose, because
MiSTer's minimal Python may omit unicodedata and the Japanese codecs.
"""

# U+FF61..U+FF9F and their full-width counterparts, in the same order.
_HALF = ('｡｢｣､･ｦｧｨｩｪｫｬｭｮｯｰｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝﾞﾟ')
_FULL = ('。「」、・ヲァィゥェォャュョッーアイウエオカキクケコサシスセソタチツテトナニヌネノ'
         'ハヒフヘホマミムメモヤユヨラリルレロワン゛゜')
_HALF_TO_FULL = dict(zip(_HALF, _FULL))

_VOICED = dict(zip('カキクケコサシスセソタチツテトハヒフヘホウ', 'ガギグゲゴザジズゼゾダヂヅデドバビブベボヴ'))
_SEMI = dict(zip('ハヒフヘホ', 'パピプペポ'))
_DAKUTEN, _HANDAKUTEN = ('゛', '゙'), ('゜', '゚')

_ROMAJI = {}
for _kana, _roman in (
        ('アイウエオ', 'A I U E O'), ('カキクケコ', 'KA KI KU KE KO'), ('サシスセソ', 'SA SHI SU SE SO'),
        ('タチツテト', 'TA CHI TSU TE TO'), ('ナニヌネノ', 'NA NI NU NE NO'),
        ('ハヒフヘホ', 'HA HI FU HE HO'), ('マミムメモ', 'MA MI MU ME MO'), ('ヤユヨ', 'YA YU YO'),
        ('ラリルレロ', 'RA RI RU RE RO'), ('ワヲン', 'WA WO N'),
        ('ガギグゲゴ', 'GA GI GU GE GO'), ('ザジズゼゾ', 'ZA JI ZU ZE ZO'),
        ('ダヂヅデド', 'DA JI ZU DE DO'), ('バビブベボ', 'BA BI BU BE BO'),
        ('パピプペポ', 'PA PI PU PE PO'), ('ヴ', 'VU'),
        ('ァィゥェォ', 'A I U E O')):
    _ROMAJI.update(zip(_kana, _roman.split()))
_PUNCTUATION = {'。': ' ', '、': ' ', '・': ' ', '「': '', '」': '', 'ー': ''}
_SMALL_Y = {'ャ': 'YA', 'ュ': 'YU', 'ョ': 'YO'}


def _to_katakana(value):
    out = []
    for char in value:
        char = _HALF_TO_FULL.get(char, char)
        if 'ぁ' <= char <= 'ゖ':  # hiragana -> katakana
            char = chr(ord(char) + 0x60)
        out.append(char)
    return out


def _join_marks(chars):
    out = []
    for char in chars:
        if out and char in _DAKUTEN and out[-1] in _VOICED:
            out[-1] = _VOICED[out[-1]]
        elif out and char in _HANDAKUTEN and out[-1] in _SEMI:
            out[-1] = _SEMI[out[-1]]
        else:
            out.append(char)
    return out


def romanize(value):
    """Return value with kana replaced by upper-case romaji; other text unchanged."""
    chars = _join_marks(_to_katakana(str(value)))
    out, double = [], False
    for index, char in enumerate(chars):
        if char == 'ッ':
            double = True
            continue
        if char in _SMALL_Y and out and len(out[-1]) > 1 and out[-1][-1] == 'I':
            # キャ -> KYA, シャ -> SHA, チャ -> CHA, ジャ -> JA
            base = out.pop()
            out.append((base[:-1] if base.endswith(('SHI', 'CHI', 'JI')) else base[:-1] + 'Y')
                       + _SMALL_Y[char][1:])
            continue
        piece = _ROMAJI.get(char)
        if piece is None:
            piece = _PUNCTUATION.get(char, char)
        elif double:
            piece = piece[0] + piece if piece[0] not in 'AIUEON' else piece
        double = False
        out.append(piece)
    return ''.join(out)
