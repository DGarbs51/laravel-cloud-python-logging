import io
import json
import logging
import os
import socket
import sys
import tempfile
import threading
from contextlib import contextmanager
from unittest.mock import patch

from laravel_cloud_logging import MonologFormatter

KEYS = ['message', 'context', 'level', 'level_name', 'channel', 'datetime', 'extra']


class Collector:
    """Unix-socket stand-in for cloud-init's log proxy: collects newline-terminated lines."""

    def __init__(self):
        self.path = os.path.join(tempfile.mkdtemp(), 'cloud-init.sock')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen()
        self.address = 'unix://' + self.path
        self.lines, self.conns = [], []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn):
        buf = b''
        try:
            while chunk := conn.recv(65536):
                buf += chunk
                *done, buf = buf.split(b'\n')
                self.lines += [json.loads(line) for line in done]
        except OSError:
            pass

    def wait(self, count):
        for _ in range(500):
            if len(self.lines) >= count:
                return self.lines
            threading.Event().wait(0.01)
        raise AssertionError(f'expected {count} lines, got {len(self.lines)}')

    def find(self, message):
        """Match by message: 3.12+ fork-with-threads warnings also become lines."""
        for _ in range(500):
            found = [line for line in self.lines if line['message'] == message]
            if found:
                return found[0]
            threading.Event().wait(0.01)
        raise AssertionError(f'no line {message!r}')

    def close(self):
        self.server.close()
        for conn in self.conns:
            conn.close()


@contextmanager
def captured_stdout():
    """Swap sys.__stdout__ (the handler's fallback) for a buffer; yields a function returning parsed lines."""
    raw = io.BytesIO()
    wrapper = io.TextIOWrapper(raw)
    with patch.object(sys, '__stdout__', wrapper):
        yield lambda: [json.loads(line) for line in raw.getvalue().splitlines()]


def fmt(**kwargs):
    record = logging.LogRecord('app.billing', kwargs.pop('level', logging.INFO), __file__, 1,
                               kwargs.pop('msg', 'hello %s'), kwargs.pop('args', ('world',)),
                               kwargs.pop('exc_info', None), sinfo=kwargs.pop('stack_info', None))
    record.__dict__.update(kwargs)
    return json.loads(MonologFormatter(channel='production').format(record))
