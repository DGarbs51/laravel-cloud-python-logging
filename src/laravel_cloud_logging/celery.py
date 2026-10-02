"""Celery: call setup(app) where the Celery app is created."""

from . import configure


def setup(app, **kwargs):
    """Stop Celery replacing root handlers and run configure(**kwargs) when it sets up logging."""
    from celery.signals import setup_logging

    app.conf.worker_hijack_root_logger = False
    # weak=False: a lambda has no other reference and would be garbage collected.
    setup_logging.connect(lambda **_: configure(**kwargs), weak=False)
