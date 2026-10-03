# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

`laravel-cloud-logging`: a zero-runtime-dependency Python package that makes Python app logs render like Laravel (Monolog JSON) logs in the Laravel Cloud dashboard. Supports Python 3.10–3.15. README.md is the user-facing spec (per-framework/server setup, options, limits) — keep it in sync with behavior changes.

## Commands

All tooling runs through `uv`.

```sh
uv run --locked scripts/check.py          # every CI gate locally, in parallel (lint, 3 type checkers, packaging, py3.10–3.15 tests, combined coverage)
uv run ruff check --fix . && uv run ruff format .   # autofix
uv run pytest -q                          # tests on the current Python
uv run pytest tests/test_core.py::test_name -q      # single test
uv run ty check && uv run mypy && uv run pyright    # type checks (all three must pass)
```

`solo.yml` defines the same commands as Solo processes (update uv/pythons/deps, sync, fix, check).

## Gates (enforced in CI, mirrored by `scripts/check.py`)

- 100% line **and** branch coverage, combined across all Python versions. A single-version local run can show version-specific branches (e.g. `sys.version_info` checks) as missed; use `scripts/check.py` for the real number.
- Strict typing in three checkers: mypy (`strict`, `disallow_any_explicit`), pyright (`strict`), ty (warnings are errors). Unused ignore comments are errors. Type checks cover `src/` only.
- Ruff: single quotes, 120-char lines. Tests are exempt from `ANN` rules.
- Packaging: wheel must declare no `Requires-Dist` and ship `py.typed`; sdist may only contain `src/`, README, LICENSE, pyproject. Never add a runtime dependency — framework imports stay inside `TYPE_CHECKING` or function bodies.

## Architecture

Nearly everything lives in `src/laravel_cloud_logging/__init__.py`:

- `MonologFormatter` turns a `LogRecord` into one JSON line with exactly seven top-level keys (`message, context, level, level_name, channel, datetime, extra`). Cloud classifies lines by their top-level keys, so never add new top-level keys — user `extra=` fields, `cloud_request_id`, `exception` and `stack` all go inside `context`. Python levels map to Monolog levels by rounding down via `_LEVELS`.
- Size cap (`_LINE`, 256 KiB): `_encode` progressively degrades (cut strings, trim trace and drop `previous` chain, drop context, cut everything) so a record always stays valid structured JSON under Cloud's 1 MB plain-text cutoff. `_clean` normalizes arbitrary values with Monolog's depth/item/string limits.
- `CloudHandler` writes to the Cloud log socket when `LARAVEL_CLOUD=1` (keeps lines from concurrent processes whole), reconnects per PID after fork, and falls back to `sys.__stdout__` with a `_RETRY` backoff. Invariant: logging never raises into the app — every failure path is suppressed or falls back.
- `configure()` builds and applies a `dictConfig` (root + `_LOGGERS` framework loggers, silences `_ACCESS` loggers unless `access_logs=True`), registers NOTICE/ALERT/EMERGENCY level names, captures warnings, installs `sys`/`threading` excepthooks, and returns the dict (Gunicorn/Hypercorn take it as `logconfig_dict`). Must be idempotent.
- Request IDs: `wsgi_middleware`, `asgi_middleware` and `django.middleware` set the `cloud_request_id` ContextVar from the `Cloud-Request-ID` header only (never `X-Request-ID`, which clients control), validated by `_headers.header_id`. WSGI/Django set it on every request so reused threads don't keep stale IDs.
- `celery.setup(app)` disables Celery's root-logger hijack and calls `configure()` from the `setup_logging` signal.

## Tests

- `tests/conftest.py` has an autouse fixture that undoes `configure()` side effects (root handlers, excepthooks, captured warnings, request ID). New global side effects in `configure()` need matching cleanup there.
- `tests/helpers.py`: `Collector` is a Unix-socket stand-in for Cloud's log proxy; `captured_stdout()` swaps `sys.__stdout__` (the handler's fallback); `fmt(**kwargs)` formats a record and returns the parsed dict.
- `tests/test_integrations.py` runs each README framework/server recipe against the real framework (from the `test` dependency group).
- `scripts/live_check.py` is a manual end-to-end check against a real Laravel Cloud environment (steps in README "Live check").

## Releasing

Bump `version` in `pyproject.toml`, `uv lock`, merge to `main`; then `gh workflow run publish.yml --ref main` (TestPyPI) and `gh release create v<version> --generate-notes` (PyPI, trusted publishing, requires approving the `pypi` environment).
