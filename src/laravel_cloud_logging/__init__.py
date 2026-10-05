"""Laravel-style logging for Python apps on Laravel Cloud. Call configure() at startup.

Records are written in the Monolog JSON shape Laravel uses on Laravel Cloud
(message, context, level, level_name, channel, datetime, extra), so levels,
exceptions and context render the same way as a Laravel app's logs. On Cloud
(LARAVEL_CLOUD=1) lines go to the platform log socket, which keeps lines from
concurrent workers whole; if the socket fails, the line goes to stdout instead.
Off Cloud, lines go to stdout, as readable lines when stdout is a terminal
(LOG_FORMAT=json|line overrides). Logging never raises into the app.
No redaction: keep secrets out of messages and extra fields.
See README.md for per-framework setup.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import io
import json
import logging
import logging.config
import math
import os
import reprlib
import socket
import sys
import threading
import time
import traceback
from array import array
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator, Mapping, MutableMapping
from datetime import datetime, timezone
from itertools import islice
from types import TracebackType
from typing import IO, TYPE_CHECKING, TypedDict, TypeVar, cast, overload

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
# color_message: uvicorn's ANSI copy of the message template, a rendering hint for its own formatter.
_STANDARD = set(logging.LogRecord('', 0, '', 0, '', (), None).__dict__) | {'message', 'asctime', 'color_message'}
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
_NODES, _CHARS = 10000, 4 * _LINE  # per-record normalization budget
_OVER = 'Over normalization budget, aborting normalization'
# A lone surrogate (from os.fsdecode, say) can't be UTF-8; inside a JSON string it becomes the valid escape \udXXX.
_ESCAPE = 'backslashreplace'
_RETRY = 5  # seconds on stdout before trying the socket again
# Set on an exception the middleware has logged, so the handler drops the server's second copy of it.
_LOGGED = '__laravel_cloud_logged__'
_GRANIAN_ERROR = 'Application callable raised an exception'  # Granian logs this from Rust, traceback as text
_FRAME = f'{os.sep}laravel_cloud_logging{os.sep}__init__.py", line '  # this module in a formatted traceback
_dumps = functools.partial(json.dumps, ensure_ascii=False, separators=(',', ':'))

__all__ = [
    'ALERT',
    'EMERGENCY',
    'NOTICE',
    'CloudHandler',
    'LineFormatter',
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


class _Normalizer:
    """Monolog's depth/item limits plus a per-record budget, so shared references can't fan out."""

    def __init__(self) -> None:
        self.nodes, self.chars = _NODES, _CHARS

    def text(self, value: str) -> str:
        if len(value) > _LINE:
            value = value[:_LINE]
        self.chars -= len(value)
        return value if self.chars >= 0 else _OVER

    def string(self, value: object) -> str:
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value[:_LINE])  # repr() of a huge buffer would be up to 4x its size
        return self.text(value if isinstance(value, str) else _str(value))

    def clean(self, value: object, depth: int = 1) -> _Json:
        self.nodes -= 1
        if self.nodes < 0 or self.chars < 0:
            return _OVER  # open containers finish with markers; nothing new is walked
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(float(value))
        if depth > _DEPTH:
            return f'Over {_DEPTH} levels deep, aborting normalization'
        if isinstance(value, dict):
            return self.clean_dict(cast('dict[object, object]', value), depth)
        if isinstance(value, (list, tuple, set, frozenset)):
            items = cast('Collection[object]', value)
            out: list[_Json] = [self.clean(v, depth + 1) for v in islice(items, _ITEMS)]
            if len(items) > _ITEMS:
                out.append(_over_items(len(items)))
            return out
        if isinstance(value, BaseException):
            self.nodes -= _TRACE  # a trace can be _TRACE frames
            data = _exception(value, depth)
            node: _Json = data
            while isinstance(node, dict):  # charge every message in the cause chain
                self.chars -= len(str(node['message']))
                node = node.get('previous')
            return data
        if isinstance(value, datetime):
            return value.isoformat()
        return self.string(value)

    def clean_dict(self, value: dict[object, object], depth: int) -> dict[str, _Json]:
        out: dict[str, _Json] = {}
        for i, (k, v) in enumerate(value.items()):
            if i == _ITEMS:
                out.setdefault('...', _over_items(len(value)))  # never overwrite a user key
                break
            key = self.string(k)
            if self.nodes < 0 or self.chars < 0:
                out.setdefault('...', _OVER)  # one marker, never overwriting a user key
                break
            out[key] = self.clean(v, depth + 1)
        return out


def _over_items(total: int) -> str:
    return f'Over {_ITEMS} items ({total} total), aborting normalization'


def _str(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return f'[unprintable {type(value).__name__}]'


_T = TypeVar('_T')


@overload
def _cut(value: str, limit: int = _STRING) -> str: ...
@overload
def _cut(value: _T, limit: int = _STRING) -> _T: ...
def _cut(value: object, limit: int = _STRING) -> object:
    """Cut a string to limit UTF-8 bytes (never mid-character), marking it when cut."""
    if not isinstance(value, str) or len(value) <= limit // 4:
        return value
    raw = value[:limit].encode(errors='surrogatepass')  # never encode more than the cut keeps
    if len(raw) <= limit and len(value) <= limit:
        return value
    return raw[:limit].decode(errors='ignore') + ' [truncated]'


class _Repr(reprlib.Repr):
    """reprlib minus its gaps: buffers are sliced before repr(); dicts and sets keep their order, never sorted."""

    def __init__(self) -> None:
        super().__init__()
        # 30 items per container, 3 levels, 1,000 characters per value, and _LINE characters in total.
        self.maxlevel = 3
        self.maxdict = self.maxlist = self.maxtuple = self.maxset = self.maxfrozenset = 30
        self.maxdeque = self.maxarray = 30
        self.maxstring = self.maxlong = self.maxother = 1000
        self.left = _LINE

    def repr1(self, x: object, level: int) -> str:
        if self.left <= 0:
            return '...'
        text = super().repr1(x, level)
        self.left -= len(text)
        return text

    def repr_bytes(self, x: bytes, level: int) -> str:
        return self.repr_str(cast('str', x), level)  # repr_str slices before repr(), and works on bytes

    repr_bytearray = repr_bytes

    def repr_dict(self, x: dict[object, object], level: int) -> str:
        pairs = (f'{self.repr1(k, level - 1)}: {self.repr1(v, level - 1)}' for k, v in x.items())
        return self._items(pairs, len(x), level, '{', '}') if x else '{}'

    def repr_set(self, x: set[object], level: int) -> str:
        return self._items((self.repr1(v, level - 1) for v in x), len(x), level, '{', '}') if x else 'set()'

    def repr_frozenset(self, x: frozenset[object], level: int) -> str:
        items = (self.repr1(v, level - 1) for v in x)
        return self._items(items, len(x), level, 'frozenset({', '})') if x else 'frozenset()'

    def _items(self, items: Iterable[str], total: int, level: int, left: str, right: str) -> str:
        if level <= 0:
            return f'{left}...{right}'
        pieces = list(islice(items, self.maxdict))
        return left + ', '.join(pieces + ['...'] * (total > self.maxdict)) + right


_CONTAINERS = (list, tuple, dict, set, frozenset, deque, array, bytes, bytearray)


def _message(exc: BaseException) -> str:
    # The built-in str() of an exception is the full repr of its container arguments, which can be any size.
    arg = exc.args[0] if len(exc.args) == 1 else exc.args or None
    builtin = type(exc).__str__ in (BaseException.__str__, KeyError.__str__)
    if builtin and isinstance(arg, _CONTAINERS):
        try:
            return _Repr().repr(arg)  # one per call: it counts what it has written
        except Exception:
            return f'[unprintable {type(exc).__name__}]'
    return _str(exc)[:_LINE]


def _exception(exc: BaseException, depth: int = 1, seen: set[int] | None = None) -> dict[str, _Json]:
    seen = seen or set()
    seen.add(id(exc))
    frames = traceback.extract_tb(exc.__traceback__, limit=-_TRACE)  # innermost _TRACE frames
    last = frames[-1] if frames else None
    data: dict[str, _Json] = {
        'class': f'{type(exc).__module__}.{type(exc).__qualname__}'.removeprefix('builtins.'),
        'message': _message(exc),
        'code': exc.args[0] if exc.args and isinstance(exc.args[0], int) and not isinstance(exc.args[0], bool) else 0,
        'file': f'{last.filename}:{last.lineno}' if last else '',
        # Innermost frame first, like PHP; 'trace' must exist for the trace view.
        'trace': [f'{f.filename}:{f.lineno} in {f.name}' for f in reversed(frames)],
    }
    cause = exc.__cause__ if exc.__cause__ is not None else (None if exc.__suppress_context__ else exc.__context__)
    if cause is not None and id(cause) not in seen and depth < _DEPTH:
        data['previous'] = _exception(cause, depth + 1, seen)
    return data


class MonologFormatter(logging.Formatter):
    def __init__(self, channel: str | None = None) -> None:
        super().__init__()
        self.channel: str = channel or os.environ.get('APP_ENV') or os.environ.get('LARAVEL_CLOUD_ENV_NAME') or 'local'

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._encode(self._record(record))
        except Exception:
            return self._encode(self._base(record, 'log record formatting failed', {}))

    def _base(self, record: logging.LogRecord, message: str, context: dict[str, _Json]) -> _Record:
        number, name = next(((n, s) for py, n, s in _LEVELS if record.levelno >= py), _LEVELS[-1][1:])
        return {
            'message': message[:_LINE],
            'context': context,
            'level': number,
            'level_name': name,
            'channel': self.channel[:_LINE],
            'datetime': datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='microseconds'),
            'extra': {'logger': record.name[:_LINE]},
        }

    def _record(self, record: logging.LogRecord) -> _Record:
        # User extra= fields stay inside context, so they can never collide with
        # the top-level keys the platform classifies on (source, logger, context, _cloud_event).
        # Platform fields are built first so neither the item limit nor the budget can drop them.
        keep: dict[str, _Json] = {}
        if request_id := cloud_request_id.get():
            keep['cloud_request_id'] = request_id[:_STRING]  # the platform's ID wins over a user extra
        if record.exc_info and record.exc_info[1] is not None:
            keep['exception'] = _exception(record.exc_info[1], 2)
        if record.stack_info:
            keep['stack'] = record.stack_info[:_LINE]
        # Extras that keep overrides are skipped, so a broken one can't fail the whole record.
        extras = ((k, v) for k, v in record.__dict__.items() if k not in _STANDARD and k not in keep)
        context: dict[object, object] = dict(islice(extras, _ITEMS + 1))  # one past the limit marks the cut
        extra = _Normalizer().clean_dict(context, 1)
        return self._base(record, record.getMessage(), {**extra, **keep})

    def _encode(self, data: _Record) -> str:
        line = _dumps(data)
        if _fits(line):
            return line
        data['message'] = _cut(data['message'])
        data['context'] = {k: _cut(v) for k, v in data['context'].items()}
        exc = data['context'].get('exception')
        if isinstance(exc, dict) and isinstance(trace := exc.get('trace'), list):  # ours, not a user dict
            exc['message'] = _cut(exc.get('message'))
            exc['trace'] = trace[:20]
            exc.pop('previous', None)
        line = _dumps(data)
        if _fits(line):
            return line
        note = f'context dropped: record exceeded {_LINE // 1024} KiB'
        keep: dict[str, _Json] = {
            k: data['context'][k] for k in ('exception', 'cloud_request_id') if k in data['context']
        }
        data['context'] = {**keep, 'truncated': note}
        line = _dumps(data)
        if _fits(line):
            return line
        # Last resort: cut every remaining free-form field, so the seven keys and the level survive.
        data['context'] = {'truncated': note}
        # Three strings must fit even when every byte becomes a six-byte JSON escape like \u0000.
        data['message'] = _cut(data['message'], _STRING // 2)
        data['channel'] = _cut(data['channel'], _STRING // 2)
        data['extra'] = {'logger': _cut(data['extra']['logger'], _STRING // 2)}
        return _dumps(data)


class LineFormatter(MonologFormatter):
    """Readable lines for a local terminal; configure() picks it off Cloud when stdout is a TTY."""

    def __init__(self, channel: str | None = None, color: bool | None = None) -> None:
        super().__init__(channel)
        self.color: bool = _color(sys.__stdout__) if color is None else color

    def format(self, record: logging.LogRecord) -> str:
        try:
            return _render(self._record(record), self.color)
        except Exception:
            return _render(self._base(record, 'log record formatting failed', {}), self.color)


# SGR codes for the readable format.
_COLORS = {
    'DEBUG': '2',
    'INFO': '34',
    'NOTICE': '36',
    'WARNING': '33',
    'ERROR': '31',
    'CRITICAL': '1;31',
    'ALERT': '1;31',
    'EMERGENCY': '1;37;41',
}
# Control characters other than newline print escaped, so a logged value can't drive the terminal.
_CONTROL = {c: f'\\x{c:02x}' for c in (*range(32), *range(127, 160)) if c != 10}


def _tty(stream: IO[str] | None) -> bool:
    with contextlib.suppress(Exception):  # closed or detached streams raise
        return stream is not None and stream.isatty()
    return False


def _color(stream: IO[str] | None) -> bool:
    return _tty(stream) and not os.environ.get('NO_COLOR')


def _text(value: object) -> str:
    text = value if isinstance(value, str) else _dumps(value)
    # Continuation lines are indented, so a newline in a value can't pass for a record of its own.
    return text.translate(_CONTROL).replace('\n', '\n    ')


def _render(data: Mapping[str, object], color: bool = False) -> str:
    """A Monolog record as one readable line, plus indented exception lines. Works on any parsed JSON object."""

    def paint(text: str, code: str) -> str:
        return f'\033[{code}m{text}\033[0m' if color else text

    name, when = _text(data.get('level_name', '')), _text(data.get('datetime', ''))
    with contextlib.suppress(ValueError):  # not ISO 8601: show it as it is
        when = datetime.fromisoformat(when).astimezone().strftime('%H:%M:%S')
    raw = data.get('context')
    context = cast('dict[str, object]', raw) if isinstance(raw, dict) else {}
    pairs = ''.join(
        f' {paint(_text(k), "2")}={_text(v)}' for k, v in context.items() if k not in ('exception', 'stack')
    )
    lines = [f'{paint(when, "2")} {paint(f"{name:<7}", _COLORS.get(name, "0"))} {_text(data.get("message", ""))}']
    lines[0] += f' {pairs}' if pairs else ''
    exc, prefix = context.get('exception'), ''
    while isinstance(exc, dict):
        exc = cast('dict[str, object]', exc)
        lines.append(f'    {prefix}{paint(_text(exc.get("class", "")), "31")}: {_text(exc.get("message", ""))}')
        if exc.get('file'):
            lines.append(f'      at {_text(exc["file"])}')
        exc, prefix = exc.get('previous'), 'Caused by '
    if isinstance(stack := context.get('stack'), str):
        lines.append('    ' + _text(stack))
    return '\n'.join(lines)


def _fits(line: str) -> bool:
    # The budget includes the newline the handler adds.
    return len(line.encode(errors=_ESCAPE)) < _LINE


class CloudHandler(logging.Handler):
    """One line per record to the Cloud log socket; stdout when off Cloud or on failure."""

    def __init__(self, address: str | None = None) -> None:
        super().__init__()
        if address is None and os.environ.get('LARAVEL_CLOUD') == '1':
            address = os.environ.get('LARAVEL_CLOUD_LOG_SOCKET') or 'unix:///tmp/cloud-init.sock'
        self.address = address
        self.sock: socket.socket | None = None
        self.pid: int | None = None
        self.retry_at: float = 0.0
        self.addFilter(_not_logged)

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
            except Exception:  # bad address, no AF_UNIX, refused: fall back to stdout for _RETRY seconds
                if sock is not None:
                    sock.close()
                self.retry_at = time.monotonic() + _RETRY
                return None
            self.sock = sock
        return self.sock

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = (self.format(record) + '\n').encode(errors=_ESCAPE)
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
                self.sock, self.retry_at = None, time.monotonic() + _RETRY
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


def _not_logged(record: logging.LogRecord) -> bool:
    """False for a server's copy of an exception the middleware already logged with the request ID."""
    if record.exc_info and getattr(record.exc_info[1], _LOGGED, False):
        return False
    # Granian passes no exc_info, only the formatted traceback; a middleware frame in it means we logged it.
    # ponytail: a middleware frame is also there for a BaseException we let through (cancellation); match the
    # exception's last line too if that ever drops a real error.
    message = record.msg if isinstance(record.msg, str) else ''
    return not (record.name.startswith('_granian') and message.startswith(_GRANIAN_ERROR) and _FRAME in message)


def _log_request_exception(exc: Exception, request_id: str | None, method: object, path: object) -> None:
    """Log an exception escaping the app once, with its request ID, before the server logs it without one."""
    token = cloud_request_id.set(request_id)
    try:
        logging.getLogger('uncaught').error('Uncaught exception in %s %s', method, path, exc_info=exc)
    finally:
        cloud_request_id.reset(token)
    with contextlib.suppress(Exception):  # an exception type that refuses new attributes just gets logged twice
        setattr(exc, _LOGGED, True)


def configure(
    level: int | str | None = None, *, exceptions: bool = True, access_logs: bool = False
) -> dict[str, object]:
    """Replace configured handlers, capture warnings, and return a dict for Gunicorn.

    Repeat calls are safe. Unknown level names fall back to INFO; numeric levels
    work too. Uncaught main/thread exceptions are logged unless exceptions=False;
    interrupts and exits are left alone. The dict is JSON-serializable, so it also
    works as a --log-config file (laravel-cloud-logging-config logging.json).
    """
    for number, _, name in _LEVELS:  # only NOTICE, ALERT and EMERGENCY are unnamed by default
        if logging.getLevelName(number) == f'Level {number}':
            logging.addLevelName(number, name)
    config = _config(level, access_logs=access_logs)
    logging.config.dictConfig(config)
    logging.captureWarnings(True)
    if exceptions:
        sys.excepthook = _uncaught
        # exc_value is only None when the hook is called by hand; both logging and sys.__excepthook__ accept that.
        threading.excepthook = lambda args: _uncaught(
            args.exc_type, cast('BaseException', args.exc_value), args.exc_traceback
        )
    return config


def _config(level: int | str | None = None, *, access_logs: bool = False, tty: bool = True) -> dict[str, object]:
    """configure()'s dictConfig dict, without applying it. tty=False ignores whether stdout is a terminal."""
    level = os.environ.get('LOG_LEVEL', 'INFO') if level is None else level
    if isinstance(level, str):
        if sys.version_info >= (3, 11):
            level = logging.getLevelNamesMapping().get(level.upper())
        else:  # no public name-to-level mapping; getLevelName's str -> int case is deprecated
            level = logging._nameToLevel.get(level.upper())  # pyright: ignore[reportPrivateUsage]
    if not isinstance(level, int):
        level = logging.INFO
    loggers: dict[str, dict[str, object]] = {
        name: {'handlers': [], 'level': level, 'propagate': True} for name in _LOGGERS + _ACCESS
    }
    if not access_logs:
        for name in _ACCESS:
            loggers[name] = {'handlers': [], 'level': logging.CRITICAL + 100, 'propagate': False}
    # Readable lines only off Cloud: there even the stdout fallback must stay JSON for the dashboard.
    choice = os.environ.get('LOG_FORMAT', '').lower()
    lines = choice == 'line' or (choice != 'json' and tty and _tty(sys.__stdout__))
    formatter = LineFormatter if lines and os.environ.get('LARAVEL_CLOUD') != '1' else MonologFormatter
    # Dotted paths, not classes: dictConfig resolves them, and the dict stays JSON for --log-config files.
    return {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {'monolog': {'()': f'{__name__}.{formatter.__name__}'}},
        'handlers': {'cloud': {'()': f'{__name__}.{CloudHandler.__name__}', 'formatter': 'monolog'}},
        'root': {'handlers': ['cloud'], 'level': level},
        'loggers': loggers,
    }


class _Body:
    """A streamed WSGI body that logs an exception raised while the server iterates it."""

    def __init__(self, body: Iterable[bytes], request_id: str | None, method: object, path: object) -> None:
        self.body = body
        self.request = (request_id, method, path)

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self.body
        except Exception as exc:
            _log_request_exception(exc, *self.request)
            raise

    def close(self) -> None:
        close = getattr(self.body, 'close', None)
        if callable(close):
            close()


def wsgi_middleware(app: WSGIApplication) -> WSGIApplication:
    """Bind the platform's Cloud-Request-ID (clients cannot set it) for each request."""

    def wrapped(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        # Set on every request (None when absent), so a reused worker thread never keeps a stale ID.
        request_id = _header_id(environ.get('HTTP_CLOUD_REQUEST_ID'))
        cloud_request_id.set(request_id)
        method, path = environ.get('REQUEST_METHOD'), environ.get('PATH_INFO')
        try:
            body = app(environ, start_response)
        except Exception as exc:
            _log_request_exception(exc, request_id, method, path)
            raise
        file_wrapper = environ.get('wsgi.file_wrapper')
        # Lists can't fail mid-body, and wrapping them or a file wrapper would cost the server its
        # Content-Length and sendfile shortcuts.
        if isinstance(body, (list, tuple)) or (isinstance(file_wrapper, type) and isinstance(body, file_wrapper)):
            return body
        return _Body(body, request_id, method, path)

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
        request_id = _header_id(raw.decode('latin-1'))
        token = cloud_request_id.set(request_id)
        try:
            return await app(scope, receive, send)
        except Exception as exc:
            _log_request_exception(exc, request_id, scope.get('method', 'WEBSOCKET'), scope.get('path'))
            raise
        finally:
            cloud_request_id.reset(token)

    return wrapped
