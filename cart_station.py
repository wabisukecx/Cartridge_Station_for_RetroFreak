"""Stable command-line entry point; implementation lives in src/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
import cart_station_cli as _application

if __name__ == '__main__':
    try:
        _application.main()
    except (Exception, KeyboardInterrupt) as error:
        print('Stopped: %s' % error, file=sys.stderr)
        sys.exit(1)
