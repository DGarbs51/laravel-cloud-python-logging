"""Print configure()'s logging config as JSON, for servers that read a --log-config file.

    python -m laravel_cloud_logging.config > logging.json

Uvicorn (with --workers) and Granian apply the file in their main process, which never imports
the app, so their boot and shutdown lines are JSON too. Reads LOG_LEVEL and LOG_FORMAT when run.
Only prints the config: the calling process's logging is left alone.
"""

from __future__ import annotations

import json
import sys

from . import _config  # pyright: ignore[reportPrivateUsage]


def main() -> None:
    json.dump(_config(), sys.stdout, indent=2)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
