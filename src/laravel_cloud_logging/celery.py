"""Celery: call setup(app) where the Celery app is created."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import configure, flush

if TYPE_CHECKING:
    from typing import TypedDict

    from celery import Celery, Task
    from typing_extensions import Unpack

    class _ConfigureKwargs(TypedDict, total=False):
        level: int | str | None
        exceptions: bool
        access_logs: bool


def _flush(**_: object) -> None:
    flush()


def setup(app: Celery[Task[[], object]], **kwargs: Unpack[_ConfigureKwargs]) -> None:
    """Stop Celery replacing root handlers and run configure(**kwargs) when it sets up logging."""
    from celery.signals import setup_logging, worker_process_shutdown

    def receiver(**_: object) -> dict[str, object]:
        return configure(**kwargs)

    app.conf.worker_hijack_root_logger = False
    # weak=False: the local function has no other reference and would be garbage collected.
    setup_logging.connect(receiver, weak=False)
    # Prefork pool children end with os._exit, which skips the atexit hook that writes queued lines.
    worker_process_shutdown.connect(_flush, weak=False)
