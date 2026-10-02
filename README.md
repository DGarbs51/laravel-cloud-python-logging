# laravel-cloud-logging

Python logging for Laravel Cloud that matches a Laravel app's logs. Levels, context and exception chains show up in the Cloud dashboard the same way they do for Laravel. The package has no runtime dependencies. It supports Python 3.10 to 3.14.

```python
from laravel_cloud_logging import configure

configure()
```

Call `configure()` once, as early as possible at startup. It:

- replaces existing handlers on the root logger and on common framework loggers (`uvicorn`, `gunicorn`, `celery`, `django`, `werkzeug`, `asyncio`, `rq.worker`, `py.warnings`), and makes those loggers propagate to root;
- captures `warnings`;
- logs uncaught exceptions (main thread and `threading`) at CRITICAL;
- silences `uvicorn.access` and `gunicorn.access`;
- returns a `logging.config.dictConfig` dict, which Gunicorn can use.

You can call it more than once. Logging never raises into your app.

```python
configure(level=None, *, exceptions=True, access_logs=False)
```

- `level`: a name (`"debug"`, `"notice"`) or a number. Default: the `LOG_LEVEL` environment variable, then `INFO`. Unknown names fall back to `INFO`.
- `exceptions=False`: do not install the uncaught-exception hooks.
- `access_logs=True`: keep app-server access logs. They are off by default because Cloud's nginx already logs every request, with its status and timing.

> **Status:** not on PyPI yet, and the package name is not final. Until it is published, install from Git:
> `pip install "laravel-cloud-logging @ git+https://github.com/DGarbs51/laravel-cloud-python-logging"`

## Framework setup

`wsgi_middleware` and `asgi_middleware` are imported from `laravel_cloud_logging`.

| Framework | Setup |
|---|---|
| Plain script | Call `configure()` before the first log call. |
| Flask | Call `configure()` before you create the app. Then set `app.wsgi_app = wsgi_middleware(app.wsgi_app)`. |
| FastAPI / Starlette | Call `configure()` and `app.add_middleware(asgi_middleware)`. Then start the server with `uvicorn.run(app, log_config=None)`. If you run several uvicorn workers, also call `configure()` in the module that defines the app. |
| Django | In `settings.py`, set `LOGGING_CONFIG = None` and call `configure()`. Add `"laravel_cloud_logging.django.middleware"` near the top of `MIDDLEWARE`. Under ASGI, you can wrap the app in `asgi.py` instead: `application = asgi_middleware(get_asgi_application())`. |
| Gunicorn | In `gunicorn.conf.py`, set `logconfig_dict = configure()`. Do not set `accesslog`. |
| Celery | Call `from laravel_cloud_logging.celery import setup; setup(app)`. This sets `worker_hijack_root_logger=False` and connects `configure()` to the `setup_logging` signal with `weak=False`. Keyword arguments are passed to `configure()`. |
| RQ | Call `configure()` before or after the worker sets up its logging. Both orders work, because `configure()` also clears the handlers on `rq.worker`. |
| `laravel-cloud-queues` | Call `configure()` before you start the worker. The worker calls `basicConfig` only when root has no handlers, so it keeps yours. Note: the worker's JSON job-event lines have no `level` or `message` today, so the dashboard shows them as plain entries. |

## Request IDs

The middleware reads the `Cloud-Request-ID` header into a `contextvars.ContextVar` (`laravel_cloud_logging.cloud_request_id`). Every record logged during the request then has `context.cloud_request_id`. The platform sets this header and replaces any value a client sends.

The package does not use `X-Request-ID`, because clients can set it and the platform passes it through. IDs longer than 128 characters are ignored.

- WSGI and Django set the variable on every request, to `None` when the header is missing. That way a reused worker thread never keeps an old ID.
- ASGI handles only `http` and `websocket` scopes, and resets the variable when the request finishes.

## Wire format

Each record is one compact JSON object on one line, in the Monolog shape that Laravel uses. The keys are always in this order:

| Key | Value |
|---|---|
| `message` | `record.getMessage()` |
| `context` | Always present (`{}` when empty). It holds every `extra=` field, plus `cloud_request_id`, `exception` and `stack` (`stack_info`). |
| `level` | Monolog number (see below) |
| `level_name` | Monolog name (see below) |
| `channel` | `APP_ENV`, then `LARAVEL_CLOUD_ENV_NAME`, then `local` |
| `datetime` | `record.created` as UTC ISO-8601 with microseconds, `+00:00`. For display only: the platform orders logs by the time it receives them. |
| `extra` | `{"logger": record.name}` |

Python levels map to Monolog levels by rounding down:

| Python level | `level` | `level_name` |
|---|---|---|
| 60 and above | 600 | EMERGENCY |
| 55 | 550 | ALERT |
| 50 (CRITICAL) | 500 | CRITICAL |
| 40 (ERROR) | 400 | ERROR |
| 30 (WARNING) | 300 | WARNING |
| 25 | 250 | NOTICE |
| 20 (INFO) | 200 | INFO |
| below 20 | 100 | DEBUG |

`configure()` registers `NOTICE` (25), `ALERT` (55) and `EMERGENCY` (60) as Python level names, but only when those numbers have no name yet. The constants are exported too: `logger.log(laravel_cloud_logging.ALERT, "...")`. The dashboard styles all eight names. The public logs API collapses them to info, warning, error and debug.

**Exceptions** go in `context.exception` as `{class, message, code, file, trace, previous}`:

- `class` is module-qualified, without `builtins.`.
- `code` is `args[0]` when it is an int (not a bool). Otherwise it is 0.
- `file` is `path:line` of the innermost frame.
- `trace` holds up to 100 `path:line in func` strings, innermost first.
- `previous` follows `__cause__`, or `__context__` unless it is suppressed. It is recursive and safe against cycles.

The dashboard shows the whole chain.

**Normalization** follows Monolog's rules:

- depth is limited to 9, and each container to 1000 items, using Monolog's marker strings;
- non-finite floats become strings;
- other objects become `str()`, and an object that cannot be printed becomes a marker.

**Size cap: 256 KiB per line.**

1. First, the message and long top-level context strings are cut to 16 KiB each, with ` [truncated]` added. The exception trace is cut to 20 frames, and `previous` is dropped.
2. If the line is still too long, only `exception`, `cloud_request_id` and a `truncated` note are kept.

The result is always valid JSON at the right level.

### Why only these seven top-level keys

The platform picks the record type from top-level keys:

- `source: "nginx-app"` makes the line a fake access log;
- `logger: "http.log.access.log0"` makes it a Caddy access log;
- `_cloud_event` takes the line out of the logs;
- `context` selects the Laravel path.

So your `extra=` fields always go inside `context`, and can never reach the top level. The platform truncates records over 1 MB, and they become plain text at info level, so the 256 KiB cap keeps a large record structured. Evidence: [SE-295](https://linear.app/laravel/issue/SE-295) and its comments.

## Transport and fallback

- **On Cloud** (`LARAVEL_CLOUD=1`), lines go to `LARAVEL_CLOUD_LOG_SOCKET`. When that variable is not set, they go to `unix:///tmp/cloud-init.sock`. Python containers do not set the variable, but the socket exists. Supported addresses are `unix://path`, `tcp://host:port` and `host:port`.
- **Why a socket:** every process in a Cloud container shares one stdout pipe. In a live test, 8 processes writing 60 KB lines to stdout corrupted 29 of 40 lines. Through the socket, all 40 lines arrived intact, because cloud-init writes one line at a time. See [SE-301](https://linear.app/laravel/issue/SE-301).
- **Connection:**
  - one `sendall` per record, under the handler lock, with a 2 s timeout;
  - it connects on the first record;
  - it reconnects after `fork()`, so Gunicorn workers never share the parent's socket;
  - after a connect or send failure, it waits 5 s before it tries again.
- **Fallback:** if the socket fails, or when you are not on Cloud, the whole line goes to `sys.__stdout__` in one write, followed by a flush.
- The platform splits socket lines over 2 MiB. The 256 KiB cap prevents this.

## Limits

- Anything printed before `configure()` runs is still plain text at info level. This includes interpreter crash output and server boot lines.
- The dashboard cannot show whether a line came from stdout or stderr.
- There is no redaction. Keep secrets out of messages and `extra=` fields.
- Not in scope: Laravel's Exceptions feature (`_cloud_event: exception`), which is Laravel-only for now.

## Development

```sh
uv run --python 3.14 --group test pytest -q
```

CI runs the tests on Python 3.10 to 3.14. The framework packages are test-only dependencies.

### Live check on Laravel Cloud

1. Run `python scripts/live_check.py command <env>`. It prints a `cpx cloud command:run` command and a marker. The command carries the package inside `--cmd`, so you do not need to deploy anything.
2. Run the printed command. It prints the marker and a `from`/`to` window. `command:run` output itself is never logged, but lines sent to the socket are.
3. Run `python scripts/live_check.py verify <app> <env> <marker> <from> <to>`. It checks:
   - every entry has type `application`, with the right levels;
   - there is exactly one exception entry, with its chain;
   - the request ID is present;
   - 40 of 40 concurrent lines arrived whole;
   - the 600 KB record is still JSON at warning level.

   The logs API returns at most 100 rows per call, so the script reads in small windows.
4. Check the dashboard Logs page by hand: the level tags and colours, and the exception chain in the details panel.

Run the check on a shared (Flex) environment and on a private one.

## License

MIT
