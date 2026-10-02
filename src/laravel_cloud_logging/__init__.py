"""Laravel-style logging for Python apps on Laravel Cloud. Call configure() at startup.

Records are written in the Monolog JSON shape Laravel uses on Laravel Cloud
(message, context, level, level_name, channel, datetime, extra), so levels,
exceptions and context render the same way as a Laravel app's logs. On Cloud
(LARAVEL_CLOUD=1) lines go to the platform log socket, which keeps lines from
concurrent workers whole; if the socket fails, the line goes to stdout instead.
Off Cloud, lines go to stdout. Logging never raises into the app.
No redaction: keep secrets out of messages and extra fields.
See README.md for per-framework setup.
"""

from __future__ import annotations

import contextlib
import contextvars
import io
import json
import logging
import logging.config
import math
import os
import socket
import sys
import threading
import time
import traceback
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from datetime import datetime, timezone
from types import TracebackType
from typing import TYPE_CHECKING, TypedDict, TypeVar, cast, overload

from ._headers import header_id as _header_id

if TYPE_CHECKING:
    from typing import TypeAlias

    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment

# Monolog levels. The dashboard styles all eight names; NOTICE, ALERT and
# EMERGENCY are registered as Python levels so apps can log them.
NOTICE, ALERT, EMERGENCY = 25, 55, 60
_LEVELS = (
    (EMERGENCY, 600, 'EMERGENCY'),
    (ALERT, 550, 'ALERT'),
    (logging.CRITICAL, 500, 'CRITICAL'),
    (logging.ERROR, 400, 'ERROR'),
    (logging.WARNING, 300, 'WARNING'),
    (NOTICE, 250, 'NOTICE'),
    (logging.INFO, 200, 'INFO'),
    (0, 100, 'DEBUG'),
)
_STANDARD = set(logging.LogRecord('', 0, '', 0, '', (), None).__dict__) | {'message', 'asctime'}
_LOGGERS = (
    'uvicorn',
    'uvicorn.error',
    'gunicorn',
    'gunicorn.error',
    'hypercorn.error',
    '_granian',
    'waitress',
    'celery',
    'django',
    'django.server',
    'werkzeug',
    'asyncio',
    'py.warnings',
    'rq.worker',
)
# nginx on Cloud already logs every request; app-server access lines would duplicate it.
_ACCESS = ('uvicorn.access', 'gunicorn.access', 'hypercorn.access', 'granian.access')
# Same limits as Monolog's normalizer, plus a size cap well under the platform's
# 1 MB truncation, which would turn the record into plain text at info level.
_DEPTH, _ITEMS, _STRING, _TRACE, _LINE = 9, 1000, 16384, 100, 256 * 1024

__all__ = [
    'ALERT',
    'EMERGENCY',
    'NOTICE',
    'CloudHandler',
    'MonologFormatter',
    'asgi_middleware',
    'cloud_request_id',
    'configure',
    'wsgi_middleware',
]

_Json: TypeAlias = bool | int | float | str | list['_Json'] | dict[str, '_Json'] | None
_Scope = MutableMapping[str, object]
_Message = MutableMapping[str, object]
_ASGIApp = Callable[[_Scope, Callable[[], Awaitable[_Message]], Callable[[_Message], Awaitable[None]]], Awaitable[None]]


class _Record(TypedDict):
    message: str
    context: dict[str, _Json]
    level: int
    level_name: str
    channel: str
    datetime: str
    extra: dict[str, str]


cloud_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar('cloud_request_id', default=None)


def _clean(value: object, depth: int = 1) -> _Json:
    """Return a JSON-safe copy, like Monolog: depth and item limits, str() fallback."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if depth > _DEPTH:
        return f'Over {_DEPTH} levels deep, aborting normalization'
    if isinstance(value, dict):
        return _clean_dict(cast('dict[object, object]', value), depth)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(cast('Iterable[object]', value))
        out: list[_Json] = [_clean(v, depth + 1) for v in items[:_ITEMS]]
        if len(items) > _ITEMS:
            out.append(f'Over {_ITEMS} items ({len(items)} total), aborting normalization')
        return out
    if isinstance(value, BaseException):
        return _exception(value, depth)
    if isinstance(value, datetime):
        return value.isoformat()
    return _str(value)


def _clean_dict(value: dict[object, object], depth: int) -> dict[str, _Json]:
    out: dict[str, _Json] = {}
    for i, (k, v) in enumerate(value.items()):
        if i == _ITEMS:
            out['...'] = f'Over {_ITEMS} items ({len(value)} total), aborting normalization'
            break
        out[str(k)] = _clean(v, depth + 1)
    return out


def _str(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return f'[unprintable {type(value).__name__}]'


_T = TypeVar('_T')


@overload
def _cut(value: str) -> str: ...
@overload
def _cut(value: _T) -> _T: ...
def _cut(value: object) -> object:
    """Cut a string to _STRING UTF-8 bytes (never mid-character), marking it when cut."""
    if not isinstance(value, str) or len(value) <= _STRING // 4 or len(value.encode()) <= _STRING:
        return value
    return value.encode()[:_STRING].decode(errors='ignore') + ' [truncated]'


def _exception(exc: BaseException, depth: int = 1, seen: set[int] | None = None) -> dict[str, _Json]:
    seen = seen or set()
    seen.add(id(exc))
    frames = traceback.extract_tb(exc.__traceback__)
    last = frames[-1] if frames else None
    data: dict[str, _Json] = {
        'class': f'{type(exc).__module__}.{type(exc).__qualname__}'.removeprefix('builtins.'),
        'message': _str(exc),
        'code': exc.args[0] if exc.args and isinstance(exc.args[0], int) and not isinstance(exc.args[0], bool) else 0,
        'file': f'{last.filename}:{last.lineno}' if last else '',
        # Innermost frame first, like PHP; 'trace' must exist for the trace view.
        'trace': [f'{f.filename}:{f.lineno} in {f.name}' for f in list(reversed(frames))[:_TRACE]],
    }
    cause = exc.__cause__ if exc.__cause__ is not None else (None if exc.__suppress_context__ else exc.__context__)
    if cause is not None and id(cause) not in seen and depth < _DEPTH:
        data['previous'] = _exception(cause, depth + 1, seen)
    return data


class MonologFormatter(logging.Formatter):
    def __init__(self, channel: str | None = None) -> None:
        super().__init__()
        self.channel = channel or os.environ.get('APP_ENV') or os.environ.get('LARAVEL_CLOUD_ENV_NAME') or 'local'

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._encode(self._record(record))
        except Exception:
            return self._encode(self._base(record, 'log record formatting failed', {}))

    def _base(self, record: logging.LogRecord, message: str, context: dict[str, _Json]) -> _Record:
        number, name = next(((n, s) for py, n, s in _LEVELS if record.levelno >= py), (100, 'DEBUG'))
        return {
            'message': message,
            'context': context,
            'level': number,
            'level_name': name,
            'channel': self.channel,
            'datetime': datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='microseconds'),
            'extra': {'logger': record.name},
        }

    def _record(self, record: logging.LogRecord) -> _Record:
        # User extra= fields stay inside context, so they can never collide with
        # the top-level keys the platform classifies on (source, logger, context, _cloud_event).
        context: dict[object, object] = {k: v for k, v in record.__dict__.items() if k not in _STANDARD}
        request_id = cloud_request_id.get()
        if request_id:
            context['cloud_request_id'] = request_id  # the platform's ID wins over a user extra
        if record.exc_info and record.exc_info[1] is not None:
            context['exception'] = record.exc_info[1]
        if record.stack_info:
            context['stack'] = record.stack_info
        return self._base(record, record.getMessage(), _clean_dict(context, 1))

    def _encode(self, data: _Record) -> str:
        line = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        if _fits(line):
            return line
        data['message'] = _cut(data['message'])
        data['context'] = {k: _cut(v) for k, v in data['context'].items()}
        exc = data['context'].get('exception')
        if isinstance(exc, dict) and isinstance(trace := exc.get('trace'), list):  # ours, not a user dict
            exc['message'] = _cut(exc.get('message'))
            exc['trace'] = trace[:20]
            exc.pop('previous', None)
        line = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        if _fits(line):
            return line
        note = 'context dropped: record exceeded 256 KiB'
        keep: dict[str, _Json] = {
            k: data['context'][k] for k in ('exception', 'cloud_request_id') if k in data['context']
        }
        data['context'] = {**keep, 'truncated': note}
        line = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        if _fits(line):
            return line
        # Last resort: cut every remaining free-form field, so the seven keys and the level survive.
        data['context'] = {'truncated': note}
        data['channel'] = _cut(data['channel'])
        data['extra'] = {'logger': _cut(data['extra']['logger'])}
        return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def _fits(line: str) -> bool:
    # The budget includes the newline the handler adds.
    return len(line.encode()) < _LINE


class CloudHandler(logging.Handler):
    """One line per record to the Cloud log socket; stdout when off Cloud or on failure."""

    def __init__(self, address: str | None = None) -> None:
        super().__init__()
        if address is None and os.environ.get('LARAVEL_CLOUD') == '1':
            address = os.environ.get('LARAVEL_CLOUD_LOG_SOCKET') or 'unix:///tmp/cloud-init.sock'
        self.address = address
        self.sock: socket.socket | None = None
        self.pid: int | None = None
        self.retry_at = 0.0

    def _connect(self) -> socket.socket | None:
        if self.pid != os.getpid():  # a forked worker must not share the parent's connection
            self.sock, self.pid = None, os.getpid()
        if self.sock is None and self.address and time.monotonic() >= self.retry_at:
            sock = None
            target: str | tuple[str, int]
            try:
                if self.address.startswith('unix://'):
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    target = self.address[len('unix://') :]
                else:
                    host, _, port = self.address.removeprefix('tcp://').rpartition(':')
                    sock, target = socket.socket(socket.AF_INET, socket.SOCK_STREAM), (host, int(port))
                sock.settimeout(2.0)
                sock.connect(target)
            except Exception:  # bad address, no AF_UNIX, refused: fall back to stdout for 5 s
                if sock is not None:
                    sock.close()
                self.retry_at = time.monotonic() + 5
                return None
            self.sock = sock
        return self.sock

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = (self.format(record) + '\n').encode()
        except Exception:
            with contextlib.suppress(Exception):  # e.g. sys.stderr closed
                self.handleError(record)
            return
        sock = self._connect()
        if sock is not None:
            try:
                sock.sendall(data)
                return
            except OSError:
                self.sock, self.retry_at = None, time.monotonic() + 5
                with contextlib.suppress(OSError):
                    sock.close()
        with contextlib.suppress(Exception):
            out = cast('io.TextIOWrapper', sys.__stdout__).buffer  # None (no stdout) raises and is ignored
            out.write(data)  # one write per line; stdout lines over 4 KiB can interleave between processes
            out.flush()

    def close(self) -> None:
        self.acquire()
        try:
            if self.sock is not None and self.pid == os.getpid():
                self.sock.close()
            self.sock = None
        finally:
            self.release()
        super().close()


def _uncaught(kind: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, value, tb)
    elif not issubclass(kind, SystemExit):
        logging.getLogger('uncaught').critical('Uncaught exception', exc_info=(kind, value, tb))


def configure(
    level: int | str | None = None, *, exceptions: bool = True, access_logs: bool = False
) -> dict[str, object]:
    """Replace configured handlers, capture warnings, and return a dict for Gunicorn.

    Repeat calls are safe. Unknown level names fall back to INFO; numeric levels
    work too. Uncaught main/thread exceptions are logged unless exceptions=False;
    interrupts and exits are left alone.
    """
    for number, name in ((NOTICE, 'NOTICE'), (ALERT, 'ALERT'), (EMERGENCY, 'EMERGENCY')):
        if logging.getLevelName(number) == f'Level {number}':
            logging.addLevelName(number, name)
    level = os.environ.get('LOG_LEVEL', 'INFO') if level is None else level
    if isinstance(level, str):
        if sys.version_info >= (3, 11):
            level = logging.getLevelNamesMapping().get(level.upper())
        else:  # no public name-to-level mapping; getLevelName's str -> int case is deprecated
            level = logging._nameToLevel.get(level.upper())  # pyright: ignore[reportPrivateUsage]
    if not isinstance(level, int):
        level = logging.INFO
    loggers: dict[str, dict[str, object]] = {
        name: {'handlers': [], 'level': level, 'propagate': True} for name in _LOGGERS
    }
    for name in _ACCESS:
        loggers[name] = (
            {'handlers': [], 'level': level, 'propagate': True}
            if access_logs
            else {'handlers': [], 'level': logging.CRITICAL + 100, 'propagate': False}
        )
    config: dict[str, object] = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {'monolog': {'()': MonologFormatter}},
        'handlers': {'cloud': {'()': CloudHandler, 'formatter': 'monolog'}},
        'root': {'handlers': ['cloud'], 'level': level},
        'loggers': loggers,
    }
    logging.config.dictConfig(config)
    logging.captureWarnings(True)
    if exceptions:
        sys.excepthook = _uncaught
        # exc_value is only None when the hook is called by hand; both logging and sys.__excepthook__ accept that.
        threading.excepthook = lambda args: _uncaught(
            args.exc_type, cast('BaseException', args.exc_value), args.exc_traceback
        )
    return config


def wsgi_middleware(app: WSGIApplication) -> WSGIApplication:
    """Bind the platform's Cloud-Request-ID (clients cannot set it) for each request."""

    def wrapped(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        # Set on every request (None when absent), so a reused worker thread never keeps a stale ID.
        cloud_request_id.set(_header_id(environ.get('HTTP_CLOUD_REQUEST_ID')))
        return app(environ, start_response)

    return wrapped


def asgi_middleware(app: _ASGIApp) -> _ASGIApp:
    """ASGI version of wsgi_middleware."""

    async def wrapped(
        scope: _Scope, receive: Callable[[], Awaitable[_Message]], send: Callable[[_Message], Awaitable[None]]
    ) -> None:
        if scope.get('type') not in ('http', 'websocket'):
            return await app(scope, receive, send)
        headers = cast('Iterable[tuple[bytes, bytes]]', scope.get('headers') or [])  # ASGI spec: (name, value) bytes
        raw = next((v for k, v in headers if k.lower() == b'cloud-request-id'), b'')
        token = cloud_request_id.set(_header_id(raw.decode('latin-1')))
        try:
            return await app(scope, receive, send)
        finally:
            cloud_request_id.reset(token)

    return wrapped
