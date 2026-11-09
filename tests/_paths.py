"""Make the project modules and data files importable from tests/.

Works in the repository and in the shipped bundle (same tests/ layout).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for directory in (ROOT, ROOT / 'src', ROOT / 'tools'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


def data(name):
    folder = 'tests/fixtures' if name.endswith('_reference_traces.json') else 'data'
    path = ROOT / folder / name
    return path if path.exists() else ROOT / name
