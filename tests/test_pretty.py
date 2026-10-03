import io
import json
import logging
import os
import runpy
import signal
import sys
from unittest.mock import patch

import pytest
from helpers import Broken

import laravel_cloud_logging
from laravel_cloud_logging import LineFormatter, MonologFormatter, configure, pretty


@pytest.fixture(autouse=True)
def restore_sigterm():
    """pretty.main forwards SIGTERM to its command; put the default back."""
    yield
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


class Tty(io.TextIOWrapper):
    def isatty(self):
        return True


def formatter_class():
    return logging.getLogger().handlers[0].formatter.__class__


@pytest.mark.parametrize(
    ('env', 'tty', 'expected'),
    [
        ({}, True, LineFormatter),
        ({}, False, MonologFormatter),
        ({'LOG_FORMAT': 'line'}, False, LineFormatter),
        ({'LOG_FORMAT': 'JSON'}, True, MonologFormatter),
        ({'LOG_FORMAT': 'line', 'LARAVEL_CLOUD': '1'}, True, MonologFormatter),  # Cloud always gets JSON
    ],
)
def test_configure_picks_format(monkeypatch, env, tty, expected):
    monkeypatch.delenv('LOG_FORMAT')
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    stream = (Tty if tty else io.TextIOWrapper)(io.BytesIO())
    with patch.object(sys, '__stdout__', stream):
        configure()
        assert formatter_class() is expected


def test_tty_survives_missing_and_closed_streams():
    closed = io.TextIOWrapper(io.BytesIO())
    closed.close()
    assert not laravel_cloud_logging._tty(None)
    assert not laravel_cloud_logging._tty(closed)


def line(color=False, **kwargs):
    record = logging.LogRecord('app', kwargs.pop('level', logging.INFO), __file__, 1, kwargs.pop('msg', 'hi'), (), None)
    record.__dict__.update(kwargs)
    return LineFormatter(color=color).format(record)


def test_line_shows_time_level_message_and_context():
    text = line(order_id=42, tags=['a'])
    assert text[8:] == ' INFO    hi  order_id=42 tags=["a"]'
    assert text[2] == text[5] == ':'


def test_line_escapes_control_characters_and_indents_newlines():
    assert line(msg='a\x1b[31mb\nfake\x9b')[8:] == ' INFO    a\\x1b[31mb\n    fake\\x9b'


def test_line_shows_exception_chain_and_stack():
    try:
        try:
            int('x')
        except ValueError as exc:
            raise RuntimeError('bad') from exc
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord('app', logging.ERROR, __file__, 1, 'failed', (), exc_info, sinfo='Stack:\n  frame')
    lines = LineFormatter(color=False).format(record).split('\n')
    assert lines[1:3] == ['    RuntimeError: bad', f'      at {__file__}:{exc_info[2].tb_lineno}']
    assert lines[3] == "    Caused by ValueError: invalid literal for int() with base 10: 'x'"
    assert lines[5:] == ['    Stack:', '      frame']


def test_line_colors(monkeypatch):
    assert '\033[31mERROR  \033[0m' in line(color=True, level=logging.ERROR)
    monkeypatch.setenv('NO_COLOR', '1')
    with patch.object(sys, '__stdout__', Tty(io.BytesIO())):
        assert not LineFormatter().color
    monkeypatch.delenv('NO_COLOR')
    with patch.object(sys, '__stdout__', Tty(io.BytesIO())):
        assert LineFormatter().color


def test_line_formatting_failure_still_renders():
    record = Broken('app', logging.INFO, __file__, 1, 'x', (), None)
    assert 'log record formatting failed' in LineFormatter(color=False).format(record)


def test_render_tolerates_any_json_object():
    text = laravel_cloud_logging._render({'message': 5, 'context': [], 'datetime': 'yesterday', 'level_name': 'ODD'})
    assert text == 'yesterday ODD     5'
    exc = {'class': 'E', 'message': 'm'}  # no file: no "at" line
    assert laravel_cloud_logging._render({'context': {'exception': exc}}).split('\n')[1:] == ['    E: m']


def record(message, level='INFO', **context):
    return json.dumps({'message': message, 'level_name': level, 'context': context, 'datetime': ''})


def run(args, stdin=b'', tty=False):
    """Run pretty.main with stdin from a pipe; returns (exit code, output)."""
    read, write = os.pipe()
    os.write(write, stdin)
    os.close(write)
    out = io.BytesIO()
    stdin, stdout = io.TextIOWrapper(io.FileIO(read)), (Tty if tty else io.TextIOWrapper)(out)
    try:
        with patch.object(sys, 'stdin', stdin), patch.object(sys, 'stdout', stdout):
            code = pretty.main(args)
    except SystemExit as exc:
        code = exc.code
    stdin.close()
    return code, out.getvalue().decode()


LINES = '\n'.join(['plain', record('one', request='r1'), record('two', 'ERROR', cloud_request_id='r2'), 'tail'])


def test_pretty_renders_records_and_passes_other_lines():
    code, out = run([], LINES.encode())
    assert code == 0
    assert out == 'plain\n INFO    one  request=r1\n ERROR   two  cloud_request_id=r2\ntail'  # unterminated stays so


@pytest.mark.parametrize(
    ('args', 'expected'),
    [
        (['--level', 'error'], [' ERROR   two  cloud_request_id=r2']),
        (['--request', 'r2'], [' ERROR   two  cloud_request_id=r2']),
        (['--grep', 'ONE'], [' INFO    one  request=r1']),
        (['--grep', 'TAI'], ['tail']),
    ],
)
def test_pretty_filters(args, expected):
    assert run(args, LINES.encode())[1].split('\n')[:-1] == expected


def test_pretty_colors_on_a_terminal(monkeypatch):
    monkeypatch.delenv('NO_COLOR', raising=False)
    assert '\033[' in run([], record('x').encode(), tty=True)[1]


def test_pretty_runs_a_command_and_keeps_its_exit_code():
    script = f'import sys; print({record("hi")!r}); print("(Pdb) ", end="", flush=True); sys.exit(3)'
    code, out = run(['--', sys.executable, '-c', script])
    assert (code, out) == (3, ' INFO    hi\n(Pdb) ')


def test_pretty_reports_signals_like_a_shell():
    assert run(['--', sys.executable, '-c', 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)'])[0] == 137


def test_pretty_missing_command():
    assert run(['--', 'definitely-not-a-command'])[0] == 127


def test_pretty_forwards_sigterm():
    script = 'import os, signal; os.kill(os.getppid(), signal.SIGTERM); signal.pause()'
    assert run(['--', sys.executable, '-c', script])[0] == 128 + signal.SIGTERM


def test_pretty_ctrl_c(monkeypatch):
    real, interrupted = os.read, []

    def read(fd, size):
        if size == 65536 and not interrupted:  # pretty's first read; Popen reads its error pipe too
            interrupted.append(fd)
            raise KeyboardInterrupt
        return real(fd, size)

    monkeypatch.setattr(pretty.os, 'read', read)
    assert run([], b'ignored')[0] == 130  # reading stdin: stop
    interrupted.clear()
    assert run(['--', sys.executable, '-c', 'print("after")']) == (0, 'after\n')  # running a command: keep reading


def test_pretty_main_module(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['pretty', '--', sys.executable, '-c', 'raise SystemExit(4)'])
    monkeypatch.delitem(sys.modules, 'laravel_cloud_logging.pretty')  # run_module executes a fresh copy
    with pytest.raises(SystemExit) as exc:
        runpy.run_module('laravel_cloud_logging.pretty', run_name='__main__')
    assert exc.value.code == 4
