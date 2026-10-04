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
uv run pyright --verifytypes laravel_cloud_logging --ignoreexternal   # public API type completeness (must be 100%)
```

`solo.yml` defines the same commands as Solo processes (update uv/pythons/deps, sync, fix, check).

Change project metadata and dependencies with `uv` commands, not by editing `pyproject.toml` or `uv.lock` by hand. The commands keep the lockfile in sync.

```sh
uv version --bump patch                   # or minor/major; updates pyproject.toml and uv.lock
uv add --group dev <pkg>                  # tooling; frameworks for integration tests go in --group test. Never a runtime dependency (see Gates)
uv remove --group dev <pkg>
uv lock --upgrade                         # bump all locked versions
uv lock --upgrade-package <pkg>           # bump one
```

Edit `pyproject.toml` directly only for settings no `uv` command manages (tool config such as ruff, mypy, coverage).

## Gates (enforced in CI, mirrored by `scripts/check.py`)

- 100% line **and** branch coverage, combined across all Python versions. A single-version local run can show version-specific branches (e.g. `sys.version_info` checks) as missed; use `scripts/check.py` for the real number.
- Strict typing in three checkers: mypy (`strict`, `disallow_any_explicit`), pyright (`strict`), ty (warnings are errors). Unused ignore comments are errors. Type checks cover `src/` only. The public API must score 100% on `pyright --verifytypes`: annotate attributes assigned in `__init__`, since inferred types can differ between checkers.
- Ruff: single quotes, 120-char lines. Tests are exempt from `ANN` rules.
- Packaging: wheel must declare no `Requires-Dist` and ship `py.typed`; sdist may only contain `src/`, README, LICENSE, pyproject, `PKG-INFO` and `.gitignore`. Never add a runtime dependency — framework imports stay inside `TYPE_CHECKING` or function bodies.

## Architecture

Nearly everything lives in `src/laravel_cloud_logging/__init__.py`:

- `MonologFormatter` turns a `LogRecord` into one JSON line with exactly seven top-level keys (`message, context, level, level_name, channel, datetime, extra`). Cloud classifies lines by their top-level keys, so never add new top-level keys — user `extra=` fields, `cloud_request_id`, `exception` and `stack` all go inside `context`. Python levels map to Monolog levels by rounding down via `_LEVELS`.
- Size cap (`_LINE`, 256 KiB): `_encode` progressively degrades (cut strings, trim trace and drop `previous` chain, drop context, cut everything) so a record always stays valid structured JSON under Cloud's 1 MB plain-text cutoff. `_clean` normalizes arbitrary values with Monolog's depth/item/string limits.
- `CloudHandler` writes to the Cloud log socket when `LARAVEL_CLOUD=1` (keeps lines from concurrent processes whole), reconnects per PID after fork, and falls back to `sys.__stdout__` with a `_RETRY` backoff. Invariant: logging never raises into the app — every failure path is suppressed or falls back.
- `LineFormatter` (readable lines via `_render`) is picked by `configure()` off Cloud when `sys.__stdout__` is a TTY, or with `LOG_FORMAT=line`; `LOG_FORMAT=json` forces JSON. On Cloud the format is always JSON, even for the stdout fallback. `_render` escapes control characters and must accept any parsed JSON object.
- `pretty.py` (`python -m laravel_cloud_logging.pretty`): pail-like viewer that renders JSON lines from stdin or a wrapped command (`-- cmd`), with `--level`, `--request`, `--grep` filters; keeps the command's exit code and forwards SIGTERM. A full-screen TUI belongs in a separate package, not here.
- `configure()` builds and applies a `dictConfig` (root + `_LOGGERS` framework loggers, silences `_ACCESS` loggers unless `access_logs=True`), registers NOTICE/ALERT/EMERGENCY level names, captures warnings, installs `sys`/`threading` excepthooks, and returns the dict (Gunicorn/Hypercorn take it as `logconfig_dict`). Must be idempotent.
- Request IDs: `wsgi_middleware`, `asgi_middleware` and `django.middleware` set the `cloud_request_id` ContextVar from the `Cloud-Request-ID` header only (never `X-Request-ID`, which clients control), validated by `_headers.header_id`. WSGI/Django set it on every request so reused threads don't keep stale IDs.
- `celery.setup(app)` disables Celery's root-logger hijack and calls `configure()` from the `setup_logging` signal.

## Tests

- `tests/conftest.py` has an autouse fixture that undoes `configure()` side effects (root handlers, excepthooks, captured warnings, request ID) and sets `LOG_FORMAT=json`, so tests parse JSON even under `pytest -s` in a terminal. New global side effects in `configure()` need matching cleanup there.
- `tests/helpers.py`: `Collector` is a Unix-socket stand-in for Cloud's log proxy; `captured_stdout()` swaps `sys.__stdout__` (the handler's fallback); `fmt(**kwargs)` formats a record and returns the parsed dict.
- `tests/test_integrations.py` runs each README framework/server recipe against the real framework (from the `test` dependency group).
- `scripts/live_check.py` is a manual end-to-end check against a real Laravel Cloud environment (steps in README "Live check").

## Releasing

Run `uv version --bump patch` (or `minor`/`major`), merge to `main`; then `gh workflow run publish.yml --ref main` (TestPyPI) and `gh release create v<version> --generate-notes` (PyPI, trusted publishing). `publish.yml` fails unless the tag equals `v$(uv version --short)`.

<!-- caveman-begin -->
Respond terse like smart caveman. All technical substance stay. Only fluff die.

Rules:
- Answer first: Answer, then reason, then next step.
- Kill ceremony: No greeting, hedging, pleasantries, recap, or closer.
- Short word: "fix" not "implement a solution for".
- Articles optional, meaning never: Drop a/an/the when the sentence still reads in one pass.
- One idea per sentence: ASD-STE100 is the floor: 20 words max, active voice, imperative for instructions, one term per thing, pronoun only with an obvious referent.
- Payload verbatim: Code blocks unchanged.
- Tool runs: bounded status: No text between routine calls.
- User's language: Compress the style, not the language.
- Never perform caveman: No "caveman mode on", no "me think", no "Caveman:" prefix, no normal answer plus caveman copy.

Switch: /caveman (default), /ultracave (fragments, each fact once), /megacave (Classical Chinese 文言文)
Stop: "stop caveman" or "normal mode"

Auto-Clarity: plain prose for security warnings, irreversible actions, step order a fragment could scramble, user confused. Resume after.

Boundaries: code, comments, commits, PRs, docs written normal.
Floor: code, commands, paths, numbers and error strings verbatim; never drop not/never/no/only.
<!-- caveman-end -->

<!-- ponytail-begin -->
# Ponytail, lazy senior dev mode

You are a lazy senior developer. Lazy means efficient, not careless. The best code is the code never written.

Before writing any code, stop at the first rung that holds:

1. Does this need to be built at all? (YAGNI)
2. Does it already exist in this codebase? Reuse the helper, util, or pattern that's already here, don't re-write it.
3. Does the standard library already do this? Use it.
4. Does a native platform feature cover it? Use it.
5. Does an already-installed dependency solve it? Use it.
6. Can this be one line? Make it one line.
7. Only then: write the minimum code that works.

The ladder runs after you understand the problem, not instead of it: read the task and the code it touches, trace the real flow end to end, then climb.

Bug fix = root cause, not symptom: a report names a symptom. Grep every caller of the function you touch and fix the shared function once — one guard there is a smaller diff than one per caller, and patching only the path the ticket names leaves a sibling caller still broken.

Rules:

- No abstractions that weren't explicitly requested.
- No new dependency if it can be avoided.
- No boilerplate nobody asked for.
- Deletion over addition. Boring over clever. Fewest files possible.
- Shortest working diff wins, but only once you understand the problem. The smallest change in the wrong place isn't lazy, it's a second bug.
- Question complex requests: "Do you actually need X, or does Y cover it?"
- Pick the edge-case-correct option when two stdlib approaches are the same size, lazy means less code, not the flimsier algorithm.
- Mark deliberate simplifications that cut a real corner with a known ceiling (global lock, O(n²) scan, naive heuristic) with a `ponytail:` comment naming the ceiling and upgrade path.

Not lazy about: understanding the problem (read it fully and trace the real flow before picking a rung, a small diff you don't understand is just laziness dressed up as efficiency), input validation at trust boundaries, error handling that prevents data loss, security, accessibility, the calibration real hardware needs (the platform is never the spec ideal, a clock drifts, a sensor reads off), anything explicitly requested. Lazy code without its check is unfinished: non-trivial logic leaves ONE runnable check behind, the smallest thing that fails if the logic breaks (an assert-based demo/self-check or one small test file; no frameworks, no fixtures). Trivial one-liners need no test.

<!-- ponytail-end -->
