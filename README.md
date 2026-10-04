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
| Uvicorn | Call `configure()` in the app module. With `--workers` above 1, also start with `--log-config logging.json`. See [Log config file](#log-config-file). |
| Hypercorn | In `hypercorn.conf.py`, set `logconfig_dict = configure()`. Start with `hypercorn -c file:hypercorn.conf.py ...`. |
| Granian | Call `configure()` in the app module. Start with `--log-config logging.json`. See [Log config file](#log-config-file). |
| Waitress | Call `configure()` in the app module. |
| Daphne | Call `configure()` in `asgi.py`. Start with `daphne -v 0 ...` to turn off its plain-text access log. |
| uWSGI / pyuwsgi | Call `configure()` in the app module. Add `--disable-logging` to turn off its plain-text access log. Add `--die-on-term`, because uWSGI 2.0 reloads on `SIGTERM` instead of stopping. |

### Log config file

The main process of Uvicorn (with several workers) and Granian never imports your app, so `configure()` does not run there. Its boot, worker and shutdown lines would be plain text. Both servers read a JSON logging config file in the main process. On Laravel Cloud, add this to your environment's build commands, after your dependencies install:

```sh
laravel-cloud-logging-config logging.json
```

The file is then part of the image every replica starts from. If the command isn't on your `PATH`, run `python -m laravel_cloud_logging.config logging.json` instead. Then start the server with the file:

```sh
uvicorn app:app --workers 4 --log-config logging.json ...
granian --interface asgi --workers 4 --log-config logging.json ... app:app
```

The file only sets up handlers. Keep the `configure()` call in your app module too: it also captures warnings and uncaught exceptions in each worker. The command reads `LOG_LEVEL` and `LOG_FORMAT` when it runs. Changing them on Cloud needs a new deployment, which rebuilds the file. The file always uses JSON lines unless `LOG_FORMAT=line`, even when you run the command in a terminal. The command only writes the file: it does not change logging in the process that runs it. Without a path, it prints the config to stdout. It is the dict that `configure()` returns with default arguments.

## Options

```python
configure(level=None, *, exceptions=True, access_logs=False)
```

- `level`: a name (`"debug"`, `"notice"`) or a number. Default: the `LOG_LEVEL` environment variable, then `INFO`. An unknown name also falls back to `INFO`.
- `exceptions=False`: do not log uncaught exceptions.
- `access_logs=True`: keep your server's access logs. They are off by default, because Cloud already logs every request.

## Local development

Off Cloud, when stdout is a terminal, `configure()` prints readable lines instead of JSON:

```
14:19:23 INFO    Order shipped  order_id=42
14:19:24 ERROR   Request failed
    RuntimeError: payment declined
      at /app/billing.py:88
    Caused by TimeoutError: timed out
      at /app/gateway.py:31
```

Piped output, CI and Cloud keep the JSON lines. Set `LOG_FORMAT=json` or `LOG_FORMAT=line` to choose. On Cloud (`LARAVEL_CLOUD=1`), lines are always JSON. Colors are on only in a terminal and only when `NO_COLOR` is not set. Control characters in messages and values print escaped.

To read JSON lines, filter them, or run your server through the viewer, use `pretty`. It works like Laravel's `php artisan pail`:

```sh
python -m laravel_cloud_logging.pretty -- uvicorn app:app --reload
python -m laravel_cloud_logging.pretty --level warning -- celery -A tasks worker
python -m laravel_cloud_logging.pretty --request 9f1c... < saved.log
```

- `--level LEVEL`: show this level and above.
- `--request ID`: show only records with this `cloud_request_id`.
- `--grep TEXT`: show only lines that contain this text. Case does not matter.

After `--`, `pretty` runs the command, reads its stdout and stderr, and exits with the command's exit code. It forwards `SIGTERM` to the command. Lines that are not records, such as server boot lines and `print()` output, pass through with control characters escaped, so log text cannot drive your terminal. `--level` and `--request` hide them. Without filters, a prompt such as `(Pdb)` shows before its newline, so `breakpoint()` works. A line longer than 256 KiB shows in pieces. Without `--`, `pretty` reads stdin.

## What `configure()` does

- Sends every log record to Cloud as one JSON line, in the Monolog format that Laravel uses. In a local terminal, it prints readable lines instead. See [Local development](#local-development).
- Takes over the root logger and common framework loggers (`uvicorn`, `gunicorn`, `django`, `celery` and others).
- Captures `warnings` and uncaught exceptions, including exceptions in threads.
- Turns off server access logs. Cloud's nginx already logs each request.
- Returns a `logging.config.dictConfig` dict for servers that accept one. The dict is plain JSON, so it also works as a [log config file](#log-config-file).

It is safe to call more than once. The formatter and handler never raise an error into your app. Python's `logging` itself still checks your arguments: see [Limits](#limits).

## Request IDs

The middleware reads the `Cloud-Request-ID` header. Every record logged during that request gets `context.cloud_request_id`, so you can find all the logs for one request. The ID is also available as the `laravel_cloud_logging.cloud_request_id` context variable.

With `wsgi_middleware` and the Django middleware, the ID stays set on the worker thread until the next request. Records logged on that thread between requests keep the last request's ID. `asgi_middleware` clears the ID when the request ends.

Cloud sets this header and replaces any value a client sends. The package ignores `X-Request-ID`, because clients control it.

Only trust the ID on Cloud. Off Cloud, nothing strips the header, so any client can set it. The middleware accepts up to 128 letters, digits, `.`, `_`, `:` and `-`. Other values leave the ID unset.

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

- Output from before `configure()` runs is plain text. This includes interpreter crashes and server boot lines, unless the server reads a [log config file](#log-config-file). uWSGI's own boot lines are always plain text.
- There is no redaction. Keep secrets out of messages and `extra=` fields.
- Python's `logging` raises `KeyError` for `extra=` keys that are `LogRecord` attributes, such as `name`, `message` or `module`. Nest them instead: `extra={'order': {'name': name}}`.
- An `extra=` field named `color_message` is dropped. Uvicorn uses it for an ANSI-colored copy of the message.
- Laravel's Exceptions feature is not supported yet.

## How it works

Each record is one JSON line with the keys `message`, `context`, `level`, `level_name`, `channel`, `datetime` and `extra`. Your `extra=` fields always go inside `context`. The package never adds other top-level keys, because Cloud uses top-level keys to choose how to parse a line.

- **Transport:** On Cloud (`LARAVEL_CLOUD=1`), lines go to the log socket (`LARAVEL_CLOUD_LOG_SOCKET`, default `unix:///tmp/cloud-init.sock`). Every process in a container shares one stdout pipe, so large lines from several processes can mix together. The socket keeps each line whole. If the socket fails, or you are not on Cloud, lines go to stdout. The format is chosen from `LARAVEL_CLOUD`, not from the transport, so the stdout fallback on Cloud is still JSON.
- **Size cap:** Each line is at most 256 KiB. Long messages and traces are cut first, then extra context. Cloud turns records over 1 MB into plain text, so this cap keeps large records structured.
- **Normalization:** Like Monolog, context stops at 9 levels deep and 1,000 items per container. Each record also has a budget of 10,000 values and about 1M characters, so shared or cyclic references cannot fan out. Values past the budget become `Over normalization budget, aborting normalization`. `cloud_request_id`, `exception` and `stack` sit outside the budget, so only the size cap can drop them. Exception messages built from containers or bytes use a bounded repr: 30 items per container, 3 levels, 1,000 characters per value and 256K characters in total. The formatter cannot bound memory used by your own `__str__` methods or `%`-format arguments.
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
4. On the dashboard Logs page, check the level colors and the exception chain. The logs API does not return the `previous` chain, so check it in the dashboard.

Run the check on a shared (Flex) environment and on a private one.

### Releasing

Publishing uses PyPI trusted publishing. See `.github/workflows/publish.yml`.

1. Run `uv version --bump patch` (or `minor`/`major`) and merge to `main`.
2. TestPyPI: `gh workflow run publish.yml --ref main`.
3. PyPI: `gh release create v<version> --generate-notes`.

## License

MIT
