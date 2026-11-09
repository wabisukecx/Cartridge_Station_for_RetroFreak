from _paths import ROOT  # noqa: F401
import json
import unittest

from cart_station_environment import MISTER_DEFAULT_OUTPUTS, PC_DEFAULT_OUTPUT
from cart_station_paths import ROOT as PROJECT_ROOT
from cart_station_service import DEFAULT_CORES


def example(name):
    return json.loads((PROJECT_ROOT / 'config' / name).read_text(encoding='utf-8-sig'))


class ShippedDefaultsTests(unittest.TestCase):
    """The code fallbacks and the shipped *.example.json files must not drift apart."""

    def test_core_fallbacks_match_example(self):
        cores = example('mister_cores.example.json')
        for system, core in DEFAULT_CORES.items():
            self.assertEqual(cores[system], core, system)

    def test_output_fallbacks_match_example(self):
        paths = example('output_paths.example.json')
        for system, directory in MISTER_DEFAULT_OUTPUTS.items():
            self.assertEqual(paths['mister'][system], directory, system)
            self.assertEqual(paths['pc'][system], PC_DEFAULT_OUTPUT, system)


if __name__ == '__main__':
    unittest.main()
