"""Write configure()'s logging config as JSON, for servers that read a --log-config file.

    laravel-cloud-logging-config logging.json
    python -m laravel_cloud_logging.config logging.json

Uvicorn (with --workers) and Granian apply the file in their main process, which never imports
the app, so their boot and shutdown lines are JSON too. Run it in the build step. Reads LOG_LEVEL
and LOG_FORMAT when run. Without a path it prints to stdout. Only writes the config: the calling
process's logging is left alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from . import _config  # pyright: ignore[reportPrivateUsage]


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog='laravel-cloud-logging-config', description='Write the logging config as JSON for --log-config.'
    )
    parser.add_argument('path', nargs='?', help='file to write, such as logging.json (default: stdout)')
    path = cast('str | None', parser.parse_args(argv).path)
    # The file configures a server later, so the terminal running this command doesn't pick the format.
    text = json.dumps(_config(tty=False), indent=2) + '\n'
    if path is None:
        sys.stdout.write(text)
    else:
        Path(path).write_text(text, encoding='utf-8')
        print(f'Wrote {path}. Start the server with --log-config {path}', file=sys.stderr)


if __name__ == '__main__':
    main()
