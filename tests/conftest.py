import logging
import sys
import threading

import pytest

import laravel_cloud_logging


@pytest.fixture(autouse=True)
def restore_logging(monkeypatch):
    """configure() installs excepthooks and root handlers; undo them so asserts print normally."""
    monkeypatch.setenv('LOG_FORMAT', 'json')  # tests parse JSON even when run with -s in a terminal
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    token = laravel_cloud_logging.cloud_request_id.set(None)  # middleware tests set it without reset
    yield
    laravel_cloud_logging.cloud_request_id.reset(token)
    sys.excepthook, threading.excepthook = sys.__excepthook__, threading.__excepthook__
    logging.captureWarnings(False)
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:], root.level = handlers, level
