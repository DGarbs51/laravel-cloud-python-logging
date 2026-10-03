#!/usr/bin/env python3
"""Run every CI gate locally: lint, type checks, packaging and the full Python test matrix in parallel,
then the combined 100% coverage gate. Prints one line per gate, and full output only for failures.

    uv run --locked scripts/check.py
"""

import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHONS = ('3.10', '3.11', '3.12', '3.13', '3.14', '3.15')
RUN = ('uv', 'run', '-q', '--locked', '--no-sync')
# Each version gets a throwaway env with only the test group, like CI; uv's cache keeps this fast.
TEST = ('uv', 'run', '-q', '--locked', '--isolated', '--no-dev', '--group', 'test')
# Anything else in the sdist is a leak; mirrors the CI packaging job.
SDIST_ALLOWED = re.compile(r'^[^/]+/(src/|README\.md$|LICENSE$|pyproject\.toml$|PKG-INFO$|\.gitignore$)')

COLOR = sys.stdout.isatty() and 'NO_COLOR' not in os.environ


def paint(text: str, code: str) -> str:
    return f'\033[{code}m{text}\033[0m' if COLOR else text


def run(cmd: tuple[str, ...]) -> tuple[bool, str]:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    return proc.returncode == 0, proc.stdout + proc.stderr


def packaging() -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as out:
        ok, output = run(('uv', 'build', '-q', '--out-dir', out))
        if not ok:
            return ok, output
        wheel, sdist = next(Path(out).glob('*.whl')), next(Path(out).glob('*.tar.gz'))
        ok, output = run(('uvx', 'twine', 'check', '--strict', str(wheel), str(sdist)))
        if not ok:
            return ok, output
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
            metadata = zf.read(next(n for n in names if n.endswith('.dist-info/METADATA'))).decode()
        with tarfile.open(sdist) as tf:
            leaked = [m.name for m in tf.getmembers() if m.isfile() and not SDIST_ALLOWED.match(m.name)]
    problems = [
        *(['wheel declares runtime dependencies'] if re.search(r'^Requires-Dist', metadata, re.MULTILINE) else []),
        *([] if any(n.endswith('laravel_cloud_logging/py.typed') for n in names) else ['wheel is missing py.typed']),
        *(f'sdist ships {name}' for name in leaked),
    ]
    if problems:
        return False, '\n'.join(problems)
    return True, f'{sdist.name.removesuffix(".tar.gz")} wheel + sdist clean'


GATES: dict[str, Callable[[], tuple[bool, str]]] = {
    'ruff check': partial(run, (*RUN, 'ruff', 'check', '.')),
    'ruff format': partial(run, (*RUN, 'ruff', 'format', '--check', '.')),
    'ty': partial(run, (*RUN, 'ty', 'check')),
    'mypy': partial(run, (*RUN, 'mypy')),
    'pyright': partial(run, (*RUN, 'pyright')),
    'packaging': packaging,
    **{
        f'py{v}': partial(
            run, (*TEST, '--python', v, 'coverage', 'run', '-m', 'pytest', '-q', '-p', 'no:cacheprovider')
        )
        for v in PYTHONS
    },
}


def timed(gate: Callable[[], tuple[bool, str]]) -> tuple[bool, str, float]:
    start = time.monotonic()
    ok, output = gate()
    return ok, output, time.monotonic() - start


def summary(output: str) -> str:
    """Last non-empty line, minus colors, pytest's '=' padding and timing."""
    lines = [line for line in re.sub(r'\x1b\[[\d;]*m|\x1b\(B', '', output).splitlines() if line.strip()]
    return re.sub(r' in [\d.]+s.*$', '', lines[-1].strip('= ')) if lines else ''


def report(name: str, ok: bool, output: str, seconds: float, detail: str | None = None) -> None:
    status = paint('ok  ', '32') if ok else paint('FAIL', '31;1')
    print(f'{status} {name:<12} {seconds:6.1f}s  {paint(detail or summary(output), "2")}', flush=True)


def main() -> int:
    failures: dict[str, str] = {}

    for name, cmd in (('sync', ('uv', 'sync', '--locked')), ('erase', (*RUN, 'coverage', 'erase'))):
        ok, output, seconds = timed(partial(run, cmd))
        if not ok:
            report(name, ok, output, seconds)
            print(output)
            return 1

    print(paint(f'running {len(GATES)} gates…', '2'), flush=True)
    with ThreadPoolExecutor(max_workers=len(GATES)) as pool:
        # map() yields in GATES order, so lines print in a fixed order as soon as each one's turn is done.
        for name, (ok, output, seconds) in zip(GATES, pool.map(timed, GATES.values()), strict=True):
            report(name, ok, output, seconds)
            if not ok:
                failures[name] = output

    if any(name.startswith('py') for name in failures):
        print(paint('skip coverage  (test failures)', '33'))
    else:
        start = time.monotonic()
        combined, output = run((*RUN, 'coverage', 'combine', '-q'))
        if combined:
            combined, output = run((*RUN, 'coverage', 'report', '--skip-covered'))
        total = re.search(r'^TOTAL.*?(\d+(?:\.\d+)?%)$', output, re.MULTILINE)
        report('coverage', combined, output, time.monotonic() - start, total and f'{total[1]} (all versions)')
        if not combined:
            failures['coverage'] = output

    for name, output in failures.items():
        print(f'\n{paint(f"── {name} ", "31;1"):─<70}\n{output.rstrip()}')
    print(paint(f'\n{len(failures)} failed', '31;1') if failures else paint('\nall gates passed', '32;1'))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
