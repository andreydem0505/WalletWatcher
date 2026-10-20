"""Run the offline suite, optionally measuring application branch coverage."""
import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'tests'
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coverage', action='store_true', help='Report line/branch coverage; require at least 90%%')
    args = parser.parse_args()
    coverage = None
    if args.coverage:
        from coverage import Coverage
        coverage = Coverage(
            branch=True, source=[str(ROOT / 'walletwatcher')],
            data_file=os.getenv('COVERAGE_FILE', str(Path(tempfile.gettempdir()) / 'walletwatcher.coverage')))
        coverage.start()
    try:
        with ExitStack() as guards:
            # Fail fast before DNS resolution or any outbound connection. Test
            # sessions and TeleBot's CUSTOM_REQUEST_SENDER still work normally.
            blocked_calls = []
            for target in ('socket.getaddrinfo', 'socket.create_connection', 'socket.socket.connect',
                           'socket.socket.connect_ex'):
                blocked_calls.append(guards.enter_context(
                    patch(target, side_effect=AssertionError('Network access is forbidden in tests'))))
            suite = unittest.defaultTestLoader.discover(str(TESTS))
            result = unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        if coverage is not None:
            coverage.stop()
            coverage.save()
    enough_coverage = coverage is None or coverage.report(show_missing=True) >= 90
    enough_autoclose_coverage = coverage is None or coverage.report(
        include=['*/trading/*.py', '*/storage/autoclose.py'], show_missing=True) >= 95
    attempted_network = any(call.called for call in blocked_calls)
    if attempted_network:
        print('FAIL: unexpected network access was attempted during tests', file=sys.stderr)
    return 0 if (result.wasSuccessful() and enough_coverage and enough_autoclose_coverage
                 and not attempted_network) else 1


if __name__ == '__main__':
    raise SystemExit(main())
