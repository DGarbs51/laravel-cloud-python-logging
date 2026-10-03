"""Readable, filterable view of laravel-cloud-logging JSON lines, like Laravel's `php artisan pail`.

    python -m laravel_cloud_logging.pretty [--level LEVEL] [--request ID] [--grep TEXT] [-- COMMAND ...]

Reads stdin, or runs COMMAND with its stdout and stderr and exits with its exit code.
Lines that are not records pass through unchanged, unless --level or --request is set.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from typing import cast

from . import _CONTROL, _ESCAPE, _LEVELS, _LINE, _color, _render  # pyright: ignore[reportPrivateUsage]

_RANK = {name: number for _, number, name in _LEVELS}


def _plain(raw: bytes) -> bytes:
    """A line that is not a record, with control characters escaped so it can't drive the terminal."""
    return raw.decode(errors='surrogateescape').translate(_CONTROL).encode(errors='surrogateescape')


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    split = argv.index('--') if '--' in argv else len(argv)
    argv, command = argv[:split], argv[split + 1 :]
    parser = argparse.ArgumentParser(prog='python -m laravel_cloud_logging.pretty', description='Readable log lines.')
    parser.add_argument('--level', type=str.upper, choices=list(_RANK)[::-1], help='show this level and above')
    parser.add_argument('--request', help='show only records with this cloud_request_id')
    parser.add_argument('--grep', type=str.casefold, help='show only lines containing this text (any case)')
    args = parser.parse_args(argv)
    level, request, grep = (
        cast('str | None', args.level),
        cast('str | None', args.request),
        cast('str | None', args.grep),
    )
    color = _color(sys.stdout)
    out = sys.stdout.buffer

    def show(line: bytes) -> None:
        try:
            data: object = json.loads(line)
        except ValueError:
            data = None
        record = cast('dict[str, object]', data) if isinstance(data, dict) else {}
        name = record.get('level_name')
        if not isinstance(name, str):
            if not (level or request or (grep and grep not in line.decode(errors='replace').casefold())):
                out.write(_plain(line) + b'\n')
            return
        context = record.get('context')
        context = cast('dict[str, object]', context) if isinstance(context, dict) else {}
        if (level and _RANK.get(name, 0) < _RANK[level]) or (request and context.get('cloud_request_id') != request):
            return
        plain = _render(record)
        if not grep or grep in plain.casefold():
            out.write(((_render(record, color) if color else plain) + '\n').encode(errors=_ESCAPE))

    proc = None
    if command:
        try:  # unbuffered, so the app's print() output keeps its place between log lines
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env={**os.environ, 'PYTHONUNBUFFERED': '1'}
            )
        except OSError as exc:
            parser.exit(127, f'{parser.prog}: {exc}\n')
        child = proc

        def stop(*_: object) -> None:
            child.terminate()

        signal.signal(signal.SIGTERM, stop)  # a supervisor stopping us stops the app too
    fd = proc.stdout.fileno() if proc and proc.stdout else sys.stdin.fileno()
    buf = b''
    while True:
        try:
            chunk = os.read(fd, 65536)
        except KeyboardInterrupt:  # the app got the same Ctrl-C: keep reading its shutdown lines
            if proc is None:
                return 130
            continue
        if not chunk:
            break
        *lines, buf = (buf + chunk).split(b'\n')
        for line in lines:
            show(line)
        if len(buf) >= _LINE:  # longer than any record: show it now, so memory stays bounded
            show(buf)
            buf = b''
        if buf and not buf.startswith(b'{') and not (level or request or grep):
            out.write(_plain(buf))  # a prompt like (Pdb) shows before its newline
            buf = b''
        out.flush()
    if buf:
        show(buf)
        out.flush()
    if proc is None:
        return 0
    code = proc.wait()
    return code if code >= 0 else 128 - code  # killed by a signal: the shell's 128 + N


if __name__ == '__main__':
    sys.exit(main())
