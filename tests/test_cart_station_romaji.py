import unittest
import _paths  # noqa: F401
from cart_station_romaji import romanize, _HALF, _FULL
from cart_station_manager import safe_text
from cart_station_md import text


class RomajiTests(unittest.TestCase):
    def test_tables_align(self):
        self.assertEqual(len(_HALF), 63)
        self.assertEqual(len(_FULL), 63)

    def test_halfwidth_katakana_header(self):
        self.assertEqual(romanize('ｻﾝﾄﾞﾗﾉﾀﾞｲﾎﾞｳｹﾝ'), 'SANDORANODAIBOUKEN')

    def test_digraphs_and_sokuon(self):
        self.assertEqual(romanize('ｷｬﾗｸﾀｰ ｼﾞｬﾝﾌﾟ'), 'KYARAKUTA JANPU')
        self.assertEqual(romanize('ガッチャマン'), 'GACCHAMAN')
        self.assertEqual(romanize('ぷよぷよ'), 'PUYOPUYO')

    def test_ascii_untouched_and_other_text_still_masked(self):
        self.assertEqual(romanize('SUPER MARIO 3'), 'SUPER MARIO 3')
        self.assertEqual(safe_text('漢字'), '??')
        self.assertEqual(safe_text('ｻﾝﾄﾞﾗ'), 'SANDORA')

    def test_header_text_keeps_katakana_without_codec(self):
        # Same bytes decode to the same kana via shift_jis or the manual fallback.
        raw = bytes.fromhex('bbdd c4de d7'.replace(' ', ''))
        self.assertEqual(text(raw, 'no-such-codec'), 'ｻﾝﾄﾞﾗ')
        self.assertEqual(text(raw, 'shift_jis'), 'ｻﾝﾄﾞﾗ')


if __name__ == '__main__':
    unittest.main()
