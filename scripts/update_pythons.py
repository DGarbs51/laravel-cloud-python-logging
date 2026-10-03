#!/usr/bin/env python3
"""Upgrade every uv-managed Python to its newest patch, then uninstall the builds the upgrade replaced.

    uv run --no-project scripts/update_pythons.py

uv keeps a minor-version link per install (cpython-3.14-macos-aarch64-none -> cpython-3.14.8-macos-aarch64-none)
and venvs point at the link, so any build a link no longer targets is safe to remove.
"""

import re
import subprocess
import sys
from pathlib import Path


def main() -> int:
    subprocess.run(('uv', 'python', 'upgrade'), check=True)
    root = Path(
        subprocess.run(
            ('uv', 'python', 'dir', '--color', 'never'), check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    current = {link.resolve() for link in root.iterdir() if link.is_symlink()}
    outdated = [
        build.name
        for build in sorted(root.iterdir())
        if build.is_dir()
        and not build.is_symlink()
        and build.resolve() not in current
        # Only builds a minor-version link has moved past: cpython-3.15.0a7-... -> cpython-3.15-...
        and (root / re.sub(r'^([a-z]+-\d+\.\d+)\.[^-+]+', r'\1', build.name)).is_symlink()
    ]
    if not outdated:
        print('No outdated Python builds to remove')
        return 0
    return subprocess.run(('uv', 'python', 'uninstall', *outdated), check=False).returncode


if __name__ == '__main__':
    sys.exit(main())
