import logging
import sys
import threading

import pytest


@pytest.fixture(autouse=True)
def restore_logging():
    """configure() installs excepthooks and root handlers; undo them so asserts print normally."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    sys.excepthook, threading.excepthook = sys.__excepthook__, threading.__excepthook__
    logging.captureWarnings(False)
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:], root.level = handlers, level
