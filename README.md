# laravel-cloud-logging

Make your Python app's logs look like a Laravel app's logs in the Laravel Cloud dashboard. You get real levels, structured context, request IDs and full exception chains, without plain-text noise.

No runtime dependencies. Python 3.10 to 3.15.

## Quick start

```sh
pip install laravel-cloud-logging
```

```python
import logging
from laravel_cloud_logging import configure

configure()

logging.getLogger(__name__).info('Order shipped', extra={'order_id': 42})
```

Call `configure()` once, as early as possible at startup. Then log with the standard `logging` module. Your app needs no other changes.

Now add the setup for your framework or server below.

## Framework setup

`wsgi_middleware` and `asgi_middleware` come from `laravel_cloud_logging`. The middleware adds the Cloud request ID to every log line. See [Request IDs](#request-ids).

| Framework | Setup |
|---|---|
| Plain script | Call `configure()` before the first log call. |
| Flask | Call `configure()` before you create the app. Then add `app.wsgi_app = wsgi_middleware(app.wsgi_app)`. |
| FastAPI / Starlette | Call `configure()` and `app.add_middleware(asgi_middleware)`. Start with `uvicorn.run(app, log_config=None)`. With several workers, also call `configure()` in the module that defines the app. |
| Django | In `settings.py`, set `LOGGING_CONFIG = None` and call `configure()`. Add `"laravel_cloud_logging.django.middleware"` near the top of `MIDDLEWARE`. |
| Celery | `from laravel_cloud_logging.celery import setup; setup(app)`. Keyword arguments go to `configure()`. |
| RQ | Call `configure()`. Before or after the worker starts, both work. |

Your server may need one more step:

| Server | Setup |
|---|---|
| Gunicorn | In `gunicorn.conf.py`, set `logconfig_dict = configure()`. Do not set `accesslog`. |
| Uvicorn | Call `configure()` in the app module. |
| Hypercorn | In `hypercorn.conf.py`, set `logconfig_dict = configure()`. Start with `hypercorn -c file:hypercorn.conf.py ...`. |
| Granian | Call `configure()` in the app module. The main process still prints its boot lines as plain text. |
| Waitress | Call `configure()` in the app module. |
| Daphne | Call `configure()` in `asgi.py`. Start with `daphne -v 0 ...` to turn off its plain-text access log. |
| uWSGI / pyuwsgi | Call `configure()` in the app module. Add `--disable-logging` to turn off its plain-text access log. Add `--die-on-term`, because uWSGI 2.0 reloads on `SIGTERM` instead of stopping. |

## Options

```python
configure(level=None, *, exceptions=True, access_logs=False)
```

- `level`: a name (`"debug"`, `"notice"`) or a number. Default: the `LOG_LEVEL` environment variable, then `INFO`.
- `exceptions=False`: do not log uncaught exceptions.
- `access_logs=True`: keep your server's access logs. They are off by default, because Cloud already logs every request.

## What `configure()` does

- Sends every log record to Cloud as one JSON line, in the Monolog format that Laravel uses.
- Takes over the root logger and common framework loggers (`uvicorn`, `gunicorn`, `django`, `celery` and others).
- Captures `warnings` and uncaught exceptions, including exceptions in threads.
- Turns off server access logs. Cloud's nginx already logs each request.
- Returns a `logging.config.dictConfig` dict for servers that accept one.

It is safe to call more than once. Logging never raises an error into your app.

## Request IDs

The middleware reads the `Cloud-Request-ID` header. Every record logged during that request gets `context.cloud_request_id`, so you can find all the logs for one request. The ID is also available as the `laravel_cloud_logging.cloud_request_id` context variable.

Cloud sets this header and replaces any value a client sends. The package ignores `X-Request-ID`, because clients control it.

## Log levels

Python levels map to Laravel levels by rounding down:

| Python | Laravel |
|---|---|
| 60 and above | EMERGENCY |
| 55 | ALERT |
| 50 (CRITICAL) | CRITICAL |
| 40 (ERROR) | ERROR |
| 30 (WARNING) | WARNING |
| 25 | NOTICE |
| 20 (INFO) | INFO |
| below 20 | DEBUG |

The `NOTICE`, `ALERT` and `EMERGENCY` constants are exported: `logger.log(laravel_cloud_logging.ALERT, "...")`.

## Exceptions

Log with `logger.exception(...)` or `exc_info=True`. The dashboard shows the exception with its class, message, file, trace and the full `previous` chain (`raise ... from ...`).

## Limits

- Output from before `configure()` runs is plain text. This includes server boot lines and interpreter crashes. uWSGI's own boot lines are always plain text.
- There is no redaction. Keep secrets out of messages and `extra=` fields.
- Laravel's Exceptions feature is not supported yet.

## How it works

Each record is one JSON line with the keys `message`, `context`, `level`, `level_name`, `channel`, `datetime` and `extra`. Your `extra=` fields always go inside `context`. The package never adds other top-level keys, because Cloud uses top-level keys to choose how to parse a line.

- **Transport:** On Cloud (`LARAVEL_CLOUD=1`), lines go to the log socket (`LARAVEL_CLOUD_LOG_SOCKET`, default `unix:///tmp/cloud-init.sock`). Every process in a container shares one stdout pipe, so large lines from several processes can mix together. The socket keeps each line whole. If the socket fails, or you are not on Cloud, lines go to stdout.
- **Size cap:** Each line is at most 256 KiB. Long messages and traces are cut first, then extra context. Cloud turns records over 1 MB into plain text, so this cap keeps large records structured.
- **Channel:** `APP_ENV`, then `LARAVEL_CLOUD_ENV_NAME`, then `local`.

## Development

```sh
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run ty check && uv run mypy && uv run pyright
uv run coverage run -m pytest -q && uv run coverage combine && uv run coverage report
```

CI runs the tests on Python 3.10 to 3.15 and requires 100% line and branch coverage across all versions combined. One local run can show version-specific branches as missed.

### Live check on Laravel Cloud

1. Run `python scripts/live_check.py command <env>`. It prints a `cpx cloud command:run` command and a marker. You do not need to deploy anything.
2. Run the printed command. It prints the marker and a `from`/`to` time window.
3. Run `python scripts/live_check.py verify <app> <env> <marker> <from> <to>`. It checks levels, the exception, the request ID, concurrent writes and the size cap.
4. On the dashboard Logs page, check the level colours and the exception chain. The logs API does not return the `previous` chain, so check it in the dashboard.

Run the check on a shared (Flex) environment and on a private one.

### Releasing

Publishing uses PyPI trusted publishing. See `.github/workflows/publish.yml`.

1. Set `version` in `pyproject.toml`, run `uv lock`, and merge to `main`.
2. TestPyPI: `gh workflow run publish.yml --ref main`.
3. PyPI: `gh release create v<version> --generate-notes`, then approve the `pypi` environment.

## License

MIT
