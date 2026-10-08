import array
import asyncio
import collections
import importlib
import json
import logging
import logging.config
import os
import runpy
import signal
import socket
import sys
import threading
import time
import tracemalloc
import warnings
from contextlib import closing
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from helpers import KEYS, Broken, Collector, captured_stdout, fmt, lines_after

import laravel_cloud_logging as lcl
from laravel_cloud_logging import CloudHandler, MonologFormatter, configure
from laravel_cloud_logging._headers import header_id


@pytest.fixture
def collector():
    with closing(Collector()) as collector:
        yield collector


def test_key_order_and_reserved_keys_stay_in_context():
    entry = fmt(order_id=1842, source='nginx-app', logger='http.log.access.log0', context='x', _cloud_event='exception')
    assert list(entry) == KEYS
    assert entry['message'] == 'hello world'
    assert entry['channel'] == 'production'
    assert entry['extra'] == {'logger': 'app.billing'}
    # Fields that would misclassify a line on Cloud stay inside context.
    assert entry['context'] == {
        'order_id': 1842,
        'source': 'nginx-app',
        'logger': 'http.log.access.log0',
        'context': 'x',
        '_cloud_event': 'exception',
    }
    assert entry['datetime'].endswith('+00:00')
    assert len(entry['datetime'].split('.')[1]) == 12
    assert fmt()['context'] == {}


@pytest.mark.parametrize(
    ('py', 'number', 'name'),
    [
        (5, 100, 'DEBUG'),
        (10, 100, 'DEBUG'),
        (20, 200, 'INFO'),
        (25, 250, 'NOTICE'),
        (30, 300, 'WARNING'),
        (40, 400, 'ERROR'),
        (50, 500, 'CRITICAL'),
        (55, 550, 'ALERT'),
        (60, 600, 'EMERGENCY'),
        (70, 600, 'EMERGENCY'),
        (-1, 100, 'DEBUG'),
        (0, 100, 'DEBUG'),
        (35, 300, 'WARNING'),
        (59, 550, 'ALERT'),
    ],
)
def test_level_mapping(py, number, name):
    entry = fmt(level=py)
    assert (entry['level'], entry['level_name']) == (number, name)


def test_channel_fallbacks():
    with patch.dict(os.environ, {'APP_ENV': 'staging', 'LARAVEL_CLOUD_ENV_NAME': 'main'}):
        assert MonologFormatter().channel == 'staging'
    with patch.dict(os.environ, {'APP_ENV': '', 'LARAVEL_CLOUD_ENV_NAME': 'main'}):
        assert MonologFormatter().channel == 'main'
    with patch.dict(os.environ, {'APP_ENV': '', 'LARAVEL_CLOUD_ENV_NAME': ''}):
        assert MonologFormatter().channel == 'local'


def test_normalization():
    class Unprintable:
        def __str__(self):
            raise ValueError

    entry = fmt(
        msg='multi\nline 雪',
        args=(),
        nan=float('nan'),
        inf=float('-inf'),
        obj=object(),
        bad=Unprintable(),
        nested={'a': {'b': (1, 2)}},
        when=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    assert entry['message'] == 'multi\nline 雪'
    assert entry['context']['when'] == '2026-01-02T03:04:05+00:00'
    assert entry['context']['nan'] == 'nan'
    assert entry['context']['inf'] == '-inf'
    assert entry['context']['obj'].startswith('<object')
    assert entry['context']['nested'] == {'a': {'b': [1, 2]}}
    assert entry['context']['bad'] == '[unprintable Unprintable]'
    deep = node = {}
    for _ in range(12):
        node['k'] = node = {}
    assert 'Over 9 levels deep, aborting normalization' in json.dumps(fmt(deep=deep)['context'])
    assert fmt(many=list(range(1500)))['context']['many'][-1] == 'Over 1000 items (1500 total), aborting normalization'
    many = fmt(many={i: i for i in range(1500)})['context']['many']
    assert len(many) == 1001
    assert many['...'].startswith('Over 1000 items')


OVER = 'Over normalization budget, aborting normalization'


def test_normalization_budget_bounds_work_and_keeps_platform_fields():
    class Leaf:
        calls = 0

        def __str__(self):
            Leaf.calls += 1
            return 'leaf'

    # Shared references fan out without a cycle: 1000**3 leaves, but the budget stops at 10,000 values.
    fan = [[[Leaf()] * 1000] * 1000] * 1000
    assert list(fmt(fan=fan)) == KEYS
    assert Leaf.calls < 10_000
    cycle = []
    cycle.extend([cycle] * 1000)  # 1000**9 paths before the depth limit
    assert list(fmt(cycle=cycle)) == KEYS

    try:
        raise ValueError('boom')
    except ValueError:
        exc_info = sys.exc_info()
    token = lcl.cloud_request_id.set('req-7')
    try:
        rows = fmt(rows=[{f'f{j}': j for j in range(10)} for _ in range(1000)], exc_info=exc_info)['context']
        wide = fmt(exc_info=exc_info, **{f's{i}': 'y' * 200_000 for i in range(6)})['context']
    finally:
        lcl.cloud_request_id.reset(token)
    # Ordinary context is cut where the budget runs out; platform fields are never dropped.
    assert rows['rows'][-1] == OVER
    assert 900 < sum(isinstance(row, dict) for row in rows['rows']) < 1000
    assert wide['s5'] == OVER
    for context in (rows, wide):
        assert context['cloud_request_id'] == 'req-7'
        assert context['exception']['message'] == 'boom'

    # Keys past the budget are never replaced by a shared marker that could overwrite a kept value.
    kept = fmt(**{OVER: 'keep me'}, **{f's{i}': 'y' * 200_000 for i in range(8)})['context']
    assert kept[OVER] == 'keep me'
    assert kept['s5'] == kept['...'] == OVER
    assert 's6' not in kept
    # A user key '...' is never overwritten by a marker either.
    dots = fmt(**{'...': 'keep me'}, **{f's{i}': 'y' * 200_000 for i in range(8)})['context']
    assert dots['...'] == 'keep me'
    assert dots['s5'] == OVER

    shared = {'k': 1}
    error = ValueError('shared')
    reused = fmt(a=(), b=(), c=shared, d=shared, error=error, exc_info=(ValueError, error, None))['context']
    assert reused['a'] == reused['b'] == []
    assert reused['c'] == reused['d'] == {'k': 1}
    assert reused['error'] == reused['exception']


@pytest.mark.parametrize('container', [list, tuple, set, frozenset])
def test_normalization_only_reads_the_item_limit(container):
    class Bounded(container):
        def __iter__(self):
            for i, value in enumerate(super().__iter__()):
                assert i < 1000, 'normalizer read beyond the item limit'
                yield value

    result = fmt(value=Bounded(range(1500)))['context']['value']
    assert len(result) == 1001
    assert result[-1] == 'Over 1000 items (1500 total), aborting normalization'


def test_huge_scalars_are_sliced_before_conversion():
    class Sliced(str):
        def encode(self, *args, **kwargs):
            assert len(self) <= 256 * 1024, 'encoded the whole oversized string'
            return super().encode(*args, **kwargs)

    text = lcl._cut(Sliced('😀' * 300_000))
    assert text.endswith(' [truncated]')
    assert len(text.encode()) <= 16384 + 12
    # Over the character budget it would become a marker; sliced first, it is kept and cut.
    assert fmt(value='x' * 2_000_000)['context']['value'].endswith(' [truncated]')

    def peak(make_record, formatter=None):
        record = make_record()  # inputs are built before measuring
        tracemalloc.start()
        try:
            line = (formatter or MonologFormatter()).format(record)
            return json.loads(line), tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    huge = b'\x00' * 12_500_000
    error = ValueError('e' * 12_500_000)
    extras = [f'k{i}' for i in range(1_000_000)]
    cases = [
        (lambda: record_with(payload=huge), None),
        (lambda: record_with(payload=bytearray(huge)), None),
        (lambda: record_with(data={huge: 1}), None),
        (lambda: record_with(exc_info=(ValueError, error, None)), None),
        (lambda: logging.LogRecord('n' * 12_500_000, logging.ERROR, '', 1, 'm', (), None), None),
        (lambda: logging.LogRecord('app', logging.ERROR, '', 1, 'x' * 12_500_000, (), None), None),
        (lambda: record_with(), MonologFormatter(channel='c' * 12_500_000)),
        (lambda: record_with(errors=[ValueError('x' * 300_000)] * 1000), None),  # exception messages use the budget
        (lambda: record_with(**dict.fromkeys(extras, 0)), None),  # only the first 1,001 extras are copied
    ]
    for make_record, formatter in cases:
        entry, used = peak(make_record, formatter)
        assert list(entry) == KEYS
        assert used < 8 * 2**20, f'{used / 2**20:.1f} MiB'

    # A memoryview keeps its opaque repr, so its contents are never copied or logged.
    view = memoryview(huge).cast('B', shape=[1, len(huge)])
    entry, used = peak(lambda: record_with(payload=view))
    assert entry['context']['payload'].startswith('<memory at ')
    assert used < 2**20


def record_with(**extra):
    record = logging.LogRecord('app', logging.ERROR, '', 1, 'm', (), extra.pop('exc_info', None))
    record.__dict__.update(extra)
    return record


def test_platform_fields_win_over_broken_extras():
    error = ValueError('real error')
    entry = fmt(exception={10**5000: 1}, exc_info=(ValueError, error, None))
    assert entry['message'] == 'hello world'
    assert entry['context']['exception']['message'] == 'real error'
    # Without exc_info the user's value is kept; an unprintable key degrades instead of failing the record.
    assert fmt(exception={10**5000: 1})['context']['exception'] == {'[unprintable int]': 1}


def test_uvicorn_color_message_is_not_context():
    entry = fmt(color_message='hello \x1b[36m%s\x1b[0m', user_id=7)
    assert entry['context'] == {'user_id': 7}


def test_exception_message_from_huge_arguments_is_bounded():
    blob = b'\x00' * 12_500_000
    fan = [[[0] * 1000] * 1000] * 1000  # str() of this exception would be about 3 GB
    huge = (
        fan,
        blob,
        bytearray(blob),
        [blob],
        array.array('B', blob),
        collections.deque([0] * 1_000_000),
        set(range(1_000_000)),
        {i: i for i in range(1_000_000)},
        [[['x' * 16384] * 30] * 30] * 30,  # each value is small, the total is not
    )
    for arg in huge:
        for error in (ValueError(arg), KeyError(arg)):
            tracemalloc.start()
            try:
                message = fmt(exc_info=(type(error), error, None))['context']['exception']['message']
                used = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            assert len(message) <= 256 * 1024
            assert used < 8 * 2**20, f'{type(arg).__name__}: {used / 2**20:.1f} MiB'
    # Small arguments read exactly like str(), in their own order.
    small = (
        ValueError(),
        ValueError('a', 'b'),
        ValueError([1, 2]),
        ValueError({'b': 1, 'a': 2}),
        ValueError({3, 1}),
        ValueError(frozenset()),
        ValueError(b'abc'),
        ValueError(collections.deque([1])),
        KeyError('x'),
        OSError(2, 'missing'),
    )
    for error in small:
        assert fmt(exc_info=(type(error), error, None))['context']['exception']['message'] == str(error)
    deep = ValueError([[[{1: 2}, {3}]]])  # past 3 levels, containers become markers
    assert fmt(exc_info=(ValueError, deep, None))['context']['exception']['message'] == '[[[{...}, {...}]]]'
    unprintable = ValueError({'k': 10**5000})
    message = fmt(exc_info=(ValueError, unprintable, None))['context']['exception']['message']
    assert 'digits' in message if sys.version_info >= (3, 13) else message == '[unprintable ValueError]'


def test_deep_cause_chain_and_float_subclass():
    error = None
    for i in range(9):
        try:
            raise ValueError(str(i)) from error
        except ValueError as raised:
            error = raised
    exc = fmt(exc_info=(ValueError, error, error.__traceback__))['context']['exception']
    while 'previous' in exc:
        assert exc['trace'], exc['message']
        exc = exc['previous']
    assert exc['trace']

    class Loud(float):
        def __str__(self):
            return 'B' * 1_000_000

    assert fmt(n=Loud('nan'))['context']['n'] == 'nan'


def test_stack_info():
    assert fmt(stack_info='Stack (most recent call last):\n  ...')['context']['stack'].startswith('Stack')


def test_exception_chain_and_code():
    try:
        try:
            raise KeyError('inner')
        except KeyError as inner:
            raise RuntimeError('outer') from inner
    except RuntimeError:
        exc_info = sys.exc_info()
    entry = fmt(level=logging.ERROR, msg='Payment failed', args=(), exc_info=exc_info)
    exc = entry['context']['exception']
    assert set(exc) == {'class', 'message', 'code', 'file', 'trace', 'previous'}
    assert exc['class'] == 'RuntimeError'
    assert exc['message'] == 'outer'
    assert exc['code'] == 0
    assert exc['file'].startswith(__file__ + ':')
    assert ' in test_exception_chain_and_code' in exc['trace'][0]
    assert exc['previous']['class'] == 'KeyError'
    assert exc['previous']['message'] == "'inner'"
    assert 'previous' not in exc['previous']

    for raised, code in ((OSError(2, 'missing'), 2), (ValueError(True), 0), (ValueError('x'), 0)):
        try:
            raise raised
        except Exception:
            assert fmt(exc_info=sys.exc_info())['context']['exception']['code'] == code

    try:
        try:
            raise KeyError('hidden')
        except KeyError:
            raise lcl.socket.timeout('suppressed') from None  # module-qualified, context suppressed
    except Exception:
        exc = fmt(exc_info=sys.exc_info())['context']['exception']
    assert exc['class'] == 'TimeoutError'
    assert 'previous' not in exc

    class Custom(Exception):
        pass

    a, b = Custom('a'), ValueError('b')
    a.__context__, b.__context__ = b, a  # a cycle must terminate
    exc = fmt(exc_info=(Custom, a, None))['context']['exception']
    assert exc['class'].endswith('test_exception_chain_and_code.<locals>.Custom')
    assert exc['previous']['class'] == 'ValueError'
    assert 'previous' not in exc['previous']
    assert exc['trace'] == []
    assert exc['file'] == ''


def test_trace_capped_at_100_frames():
    def recurse(n):
        if n == 0:
            raise ValueError('deep')
        recurse(n - 1)

    try:
        recurse(150)
    except ValueError:
        trace = fmt(exc_info=sys.exc_info())['context']['exception']['trace']
    assert len(trace) == 100
    assert ' in recurse' in trace[0]


def test_size_cap():
    def line(entry):
        return len(json.dumps(entry, ensure_ascii=False, separators=(',', ':')).encode())

    big = fmt(level=logging.ERROR, big='x' * 600_000, small='ok')
    assert line(big) <= 256 * 1024
    assert list(big) == KEYS
    assert big['level_name'] == 'ERROR'
    assert big['message'] == 'hello world'
    assert big['context']['small'] == 'ok'
    assert big['context']['big'].endswith(' [truncated]')
    assert len(big['context']['big']) == 16384 + 12

    msg = fmt(msg='y' * 600_000, args=())
    assert line(msg) <= 256 * 1024
    assert msg['message'].endswith(' [truncated]')

    try:
        raise ValueError('boom')
    except ValueError:
        exc_info = sys.exc_info()
    token = lcl.cloud_request_id.set('req-9')
    try:
        many = fmt(level=logging.CRITICAL, blob=['z' * 1000] * 900, exc_info=exc_info)
    finally:
        lcl.cloud_request_id.reset(token)
    assert line(many) <= 256 * 1024
    assert many['level_name'] == 'CRITICAL'
    assert set(many['context']) == {'exception', 'cloud_request_id', 'truncated'}
    assert many['context']['exception']['message'] == 'boom'

    wide = fmt(**{f'k{i}': 'w' * 15000 for i in range(30)})  # many fields under 16 KiB each
    assert line(wide) <= 256 * 1024
    assert 'truncated' in wide['context']


def test_formatter_failure_still_emits_json():
    record = Broken('x', logging.WARNING, __file__, 1, 'm', (), None)
    entry = json.loads(MonologFormatter().format(record))
    assert entry['message'] == 'log record formatting failed'
    assert entry['level_name'] == 'WARNING'


def test_socket_transport_concurrency_and_repeat_configure(collector):
    env = {'LARAVEL_CLOUD': '1', 'LARAVEL_CLOUD_LOG_SOCKET': collector.address, 'LOG_LEVEL': 'debug'}
    with patch.dict(os.environ, env), captured_stdout() as stdout:
        configure()
        config = configure()
        assert config['handlers']['cloud']['()'] == 'laravel_cloud_logging.CloudHandler'
        assert len(logging.getLogger().handlers) == 1
        assert logging.getLogger().level == logging.DEBUG

        logging.getLogger('uvicorn.error').info('routed')
        logging.getLogger('uvicorn.access').info('GET / 200')  # nginx already logs requests
        logging.getLogger('gunicorn.access').info('GET / 200')
        logging.log(lcl.NOTICE, 'notice level')
        threads = [
            threading.Thread(target=lambda i=i: [logging.info('t%s %s', i, 'p' * 20000) for _ in range(5)])
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = collector.wait(42)
        assert [line['message'] for line in lines[:2]] == ['routed', 'notice level']
        assert lines[1]['level_name'] == 'NOTICE'
        assert not any('GET /' in line['message'] for line in lines)
        concurrent = [line for line in lines if line['message'].startswith('t')]
        assert len(concurrent) == 40
        assert all(len(line['message']) == 20003 for line in concurrent)
        assert stdout() == []


def test_config_dict_round_trips_through_json_and_dictconfig():
    config = json.loads(json.dumps(configure(exceptions=False)))
    logging.getLogger().handlers.clear()
    logging.config.dictConfig(config)
    assert len(logging.getLogger().handlers) == 1
    assert isinstance(logging.getLogger().handlers[0], CloudHandler)
    assert isinstance(logging.getLogger().handlers[0].formatter, MonologFormatter)


def test_config_module_prints_the_config_without_applying_it(monkeypatch, capsys):
    root = logging.getLogger()
    before = root.handlers[:]
    importlib.import_module('laravel_cloud_logging.config')  # importing prints nothing
    assert capsys.readouterr().out == ''
    monkeypatch.setattr(sys, 'argv', ['laravel-cloud-logging-config'])
    monkeypatch.delitem(sys.modules, 'laravel_cloud_logging.config')  # run it fresh, as python -m does
    runpy.run_module('laravel_cloud_logging.config', run_name='__main__')
    config = json.loads(capsys.readouterr().out)
    assert config['formatters'] == {'monolog': {'()': 'laravel_cloud_logging.MonologFormatter'}}
    assert root.handlers == before
    assert sys.excepthook is sys.__excepthook__


def test_config_command_writes_json_even_from_a_terminal(tmp_path, monkeypatch, capsys):
    from laravel_cloud_logging import config

    monkeypatch.delenv('LOG_FORMAT')
    monkeypatch.setattr(lcl, '_tty', lambda stream: True)
    config.main([str(tmp_path / 'logging.json')])
    written = json.loads((tmp_path / 'logging.json').read_text())
    assert written['formatters'] == {'monolog': {'()': 'laravel_cloud_logging.MonologFormatter'}}
    assert 'Wrote' in capsys.readouterr().err


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='needs fork')
def test_fork_reconnects(collector):
    with patch.dict(os.environ, {'LARAVEL_CLOUD': '1', 'LARAVEL_CLOUD_LOG_SOCKET': collector.address}):
        configure(exceptions=False)
        logging.info('from parent')
        collector.find('from parent')
        parent_sock = logging.getLogger().handlers[0].sock
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)  # 3.12+: fork with threads
            pid = os.fork()
        if pid == 0:
            handler = logging.getLogger().handlers[0]
            logging.info('from child')
            drained = lcl.flush(5)  # os._exit skips atexit, so nothing else drains the queue
            os._exit(0 if drained and handler.sock not in (None, parent_sock) and handler.pid == os.getpid() else 1)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        collector.find('from child')
        assert len(collector.conns) == 2


def test_request_id_in_context(collector):
    with patch.dict(os.environ, {'LARAVEL_CLOUD': '1', 'LARAVEL_CLOUD_LOG_SOCKET': collector.address}):
        configure(exceptions=False)
        token = lcl.cloud_request_id.set('req-1')
        logging.info('in request', extra={'cloud_request_id': 'user-set'})
        logging.info('in request 2')
        lcl.cloud_request_id.reset(token)
        logging.info('after request')
        assert collector.find('in request')['context'] == {'cloud_request_id': 'req-1'}  # platform ID wins
        assert collector.find('in request 2')['context'] == {'cloud_request_id': 'req-1'}
        assert collector.find('after request')['context'] == {}


def test_fallback_to_stdout_after_socket_loss_and_retry_delay(collector):
    with (
        patch.dict(os.environ, {'LARAVEL_CLOUD': '1', 'LARAVEL_CLOUD_LOG_SOCKET': collector.address}),
        captured_stdout() as stdout,
    ):
        configure(exceptions=False)
        logging.info('before')
        collector.find('before')
        collector.close()  # socket gone: the next lines go to stdout, never raise
        handler = logging.getLogger().handlers[0]
        handler.sock.close()
        logging.error('after socket loss')
        logging.warning('during retry delay')
        assert lcl.flush(5)
        assert handler.sock is None
        assert handler.retry_at > 0
        sys.__stdout__.flush()
        assert [(e['message'], e['level_name']) for e in stdout()] == [
            ('after socket loss', 'ERROR'),
            ('during retry delay', 'WARNING'),
        ]


def test_unreachable_or_invalid_address_never_raises():
    for address in ('unix:///nonexistent/cloud-init.sock', 'tcp://127.0.0.1:notaport', 'nonsense'):
        with captured_stdout() as stdout:
            logger = logging.getLogger(f'test.{address}')
            handler = CloudHandler(address)
            handler.setFormatter(MonologFormatter())
            logger.addHandler(handler)
            logger.propagate = False
            logger.warning('still logged')
            sys.__stdout__.flush()
            assert stdout()[0]['message'] == 'still logged'
            logger.removeHandler(handler)


def test_default_socket_path_on_cloud():
    with patch.dict(os.environ, {'LARAVEL_CLOUD': '1'}):
        os.environ.pop('LARAVEL_CLOUD_LOG_SOCKET', None)
        assert CloudHandler().address == 'unix:///tmp/cloud-init.sock'
    with patch.dict(os.environ, {'LARAVEL_CLOUD': '0', 'LARAVEL_CLOUD_LOG_SOCKET': 'unix:///x'}):
        assert CloudHandler().address is None


def test_tcp_transport():
    server = socket.create_server(('127.0.0.1', 0))
    port = server.getsockname()[1]
    handler = CloudHandler(f'tcp://127.0.0.1:{port}')
    handler.setFormatter(MonologFormatter())
    handler.handle(logging.LogRecord('tcp', logging.INFO, __file__, 1, 'over tcp', (), None))
    conn, _ = server.accept()
    conn.settimeout(2)
    assert json.loads(conn.recv(65536))['message'] == 'over tcp'
    handler.close()
    conn.close()
    server.close()


def test_off_cloud_writes_stdout():
    with patch.dict(os.environ, {'LARAVEL_CLOUD': ''}), captured_stdout() as stdout:
        configure(exceptions=False)
        logging.warning('local %s', 'dev')
        sys.__stdout__.flush()
        assert stdout()[0]['message'] == 'local dev'
        assert stdout()[0]['level_name'] == 'WARNING'


def test_access_logs_opt_in_level_names_and_warnings():
    with patch.dict(os.environ, {'LARAVEL_CLOUD': ''}), captured_stdout() as stdout:
        configure(exceptions=False, access_logs=True, level='notice')
        assert logging.getLevelName(25) == 'NOTICE'
        assert logging.getLogger().level == 25
        logging.getLogger('uvicorn.access').log(lcl.ALERT, 'GET / 500')
        logging.info('below level')
        warnings.warn('deprecated thing', UserWarning, stacklevel=2)
        sys.__stdout__.flush()
        entries = stdout()
        assert entries[0]['message'] == 'GET / 500'
        assert entries[0]['level_name'] == 'ALERT'
        assert entries[1]['extra'] == {'logger': 'py.warnings'}
        assert 'deprecated thing' in entries[1]['message']
        assert len(entries) == 2
    configure(level='bogus', exceptions=False)
    assert logging.getLogger().level == logging.INFO
    configure(level=logging.ERROR, exceptions=False)
    assert logging.getLogger().level == logging.ERROR


def test_existing_level_names_are_kept():
    logging.addLevelName(25, 'SUCCESS')
    try:
        configure(exceptions=False)
        assert logging.getLevelName(25) == 'SUCCESS'
    finally:
        logging.addLevelName(25, 'NOTICE')


def test_replaces_framework_handlers():
    stray = logging.StreamHandler()
    for name in ('django', 'rq.worker'):
        logging.getLogger(name).addHandler(stray)
    configure(exceptions=False)
    for name in ('django', 'rq.worker'):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate


def test_uncaught_exception_hooks():
    with patch.dict(os.environ, {'LARAVEL_CLOUD': ''}), captured_stdout() as stdout:
        configure()
        try:
            raise ValueError('crash')
        except ValueError:
            sys.excepthook(*sys.exc_info())
        thread = threading.Thread(target=lambda: 1 / 0)
        thread.start()
        thread.join()
        sys.excepthook(SystemExit, SystemExit(0), None)  # exits are left alone
        with patch.object(sys, '__excepthook__') as default:
            sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
        default.assert_called_once()
        sys.__stdout__.flush()
        entries = stdout()
        assert [e['context']['exception']['class'] for e in entries] == ['ValueError', 'ZeroDivisionError']
        assert all(e['level_name'] == 'CRITICAL' for e in entries)
    hook = sys.excepthook
    configure(exceptions=False)
    assert sys.excepthook is hook  # exceptions=False does not install (or remove) hooks


def test_wsgi_middleware():
    seen = []

    def app(environ, start_response):
        seen.append(lcl.cloud_request_id.get())
        return [b'ok']

    wrapped = lcl.wsgi_middleware(app)
    assert list(wrapped({'HTTP_CLOUD_REQUEST_ID': 'abc'}, None)) == [b'ok']
    wrapped({'HTTP_X_REQUEST_ID': 'client-set'}, None)  # X-Request-ID is client-controlled: ignored
    wrapped({'HTTP_CLOUD_REQUEST_ID': 'x' * 129}, None)
    assert seen == ['abc', None, None]


@pytest.mark.parametrize('value', ['req-1', 'a' * 128, '9f1c.b2:c_d-E'])
def test_header_id_accepts_ids(value):
    assert header_id(value) == value


@pytest.mark.parametrize('value', [None, b'abc', '', 'x' * 129, 'a b', 'a\nb', '\x1b[2J', 'a\x00', 'é', 'abc\n'])
def test_header_id_rejects_unsafe_values(value):
    assert header_id(value) is None


def test_asgi_middleware():
    seen = []

    async def app(scope, receive, send):
        seen.append(lcl.cloud_request_id.get())

    wrapped = lcl.asgi_middleware(app)

    async def run():
        await wrapped({'type': 'http', 'headers': [(b'Cloud-Request-ID', b'def')]}, None, None)
        seen.append(lcl.cloud_request_id.get())  # reset after the request
        await wrapped({'type': 'websocket', 'headers': [(b'cloud-request-id', b'ws')]}, None, None)
        await wrapped({'type': 'http', 'headers': [(b'x-request-id', b'client-set')]}, None, None)
        token = lcl.cloud_request_id.set('outer')
        await wrapped({'type': 'lifespan'}, None, None)  # not touched
        lcl.cloud_request_id.reset(token)

    asyncio.run(run())
    assert seen == ['def', None, 'ws', None, 'outer']


def test_middleware_logs_uncaught_exceptions_once_with_the_request_id():
    async def asgi_app(scope, receive, send):
        raise RuntimeError('asgi boom')

    def wsgi_app(environ, start_response):
        raise RuntimeError('wsgi boom')

    def server_logs(exc):  # what uvicorn, gunicorn and the others do after the middleware re-raises
        logging.getLogger('uvicorn.error').error('Exception in ASGI application', exc_info=exc)

    def run():
        configure(exceptions=False)
        with pytest.raises(RuntimeError) as asgi:
            asyncio.run(
                lcl.asgi_middleware(asgi_app)(
                    {'type': 'http', 'method': 'GET', 'path': '/a', 'headers': [(b'cloud-request-id', b'r-1')]},
                    None,
                    None,
                )
            )
        server_logs(asgi.value)
        with pytest.raises(RuntimeError) as wsgi:
            lcl.wsgi_middleware(wsgi_app)({'REQUEST_METHOD': 'POST', 'PATH_INFO': '/w'}, None)
        server_logs(wsgi.value)
        with pytest.raises(RuntimeError):  # nested middleware: the outer copy is dropped too
            asyncio.run(
                lcl.asgi_middleware(lcl.asgi_middleware(asgi_app))(
                    {'type': 'websocket', 'path': '/ws', 'headers': []}, None, None
                )
            )

    entries = lines_after(run)
    assert [(e['message'], e['context'].get('cloud_request_id')) for e in entries] == [
        ('Uncaught exception in GET /a', 'r-1'),
        ('Uncaught exception in POST /w', None),
        ('Uncaught exception in WEBSOCKET /ws', None),
    ]
    assert all(e['context']['exception']['class'] == 'RuntimeError' for e in entries)


def test_wsgi_middleware_logs_an_exception_while_streaming_the_body():
    closed = []

    class Stream:
        def __iter__(self):
            yield b'part'
            raise RuntimeError('mid-body')

        def close(self):
            closed.append(True)

    def run():
        configure(exceptions=False)
        body = lcl.wsgi_middleware(lambda environ, start_response: Stream())(
            {'HTTP_CLOUD_REQUEST_ID': 's-1', 'REQUEST_METHOD': 'GET', 'PATH_INFO': '/s'}, None
        )
        lcl.cloud_request_id.set('next-request')  # the server may iterate after the ID moved on
        with pytest.raises(RuntimeError):
            list(body)
        body.close()
        lcl.wsgi_middleware(lambda environ, start_response: iter([b'ok']))({}, None).close()  # no close(): fine

    [entry] = lines_after(run)
    assert entry['message'] == 'Uncaught exception in GET /s'
    assert entry['context']['cloud_request_id'] == 's-1'
    assert closed == [True]


def test_wsgi_middleware_returns_lists_and_file_wrappers_unwrapped():
    class FileWrapper:
        def __init__(self, f):
            self.f = f

    def app(environ, start_response):
        return environ['wsgi.file_wrapper'](None) if 'wsgi.file_wrapper' in environ else [b'ok']

    wrapped = lcl.wsgi_middleware(app)
    assert wrapped({}, None) == [b'ok']
    assert isinstance(wrapped({'wsgi.file_wrapper': FileWrapper}, None), FileWrapper)
    assert type(wrapped({'wsgi.file_wrapper': lambda f: iter(())}, None)) is lcl._Body  # a function, not a type


def test_handler_drops_granian_text_copies_only_when_the_middleware_saw_the_exception():
    def granian_text(frames):
        return logging.LogRecord('_granian.utils', logging.ERROR, '', 0, f'{lcl._GRANIAN_ERROR}\n{frames}', (), None)

    ours = f'Traceback ...\n  File "{lcl.__file__}", line 9, in wrapped'
    assert lcl._not_logged(granian_text(ours)) is False
    assert lcl._not_logged(granian_text('Traceback ...\n  File "/app/app.py", line 3')) is True  # no middleware
    assert lcl._not_logged(logging.LogRecord('_granian', logging.ERROR, '', 0, 42, (), None)) is True

    class Frozen(Exception):
        def __setattr__(self, name, value):
            raise AttributeError(name)

    with captured_stdout():  # an exception that refuses the mark is logged again by the server, not lost
        configure(exceptions=False)
        lcl._log_request_exception(Frozen(), None, 'GET', '/')
    assert not hasattr(Frozen(), lcl._LOGGED)


def test_review_regressions_in_formatter():
    class BrokenStr(Exception):
        def __str__(self):
            raise ValueError

    exc = fmt(exc_info=(BrokenStr, BrokenStr(), None), order=1)['context']
    assert exc['order'] == 1
    assert exc['exception']['message'] == '[unprintable BrokenStr]'

    class Falsey(Exception):
        def __bool__(self):
            return False

    outer = RuntimeError('outer')
    outer.__cause__ = Falsey('cause')
    assert fmt(exc_info=(RuntimeError, outer, None))['context']['exception']['previous']['class'].endswith('Falsey')

    user_exception = fmt(level=logging.ERROR, blob='b' * 300_000, exception={'domain': 'user'})
    assert user_exception['message'] == 'hello world'
    assert user_exception['context']['exception'] == {'domain': 'user'}


def test_size_cap_counts_bytes_and_marks_cuts():
    emoji = fmt(msg='😀' * 100_000, args=())['message']
    assert emoji.endswith(' [truncated]')
    assert len(emoji.encode()) <= 16384 + 12

    try:
        raise ValueError('z' * 300_000)
    except ValueError:
        exc = fmt(exc_info=sys.exc_info())['context']['exception']
    assert exc['message'].endswith(' [truncated]')
    assert len(exc['message']) == 16384 + 12


def test_handler_lines_never_exceed_256_kib():
    with captured_stdout() as stdout:
        handler = CloudHandler(None)
        handler.address = None  # stdout, even if the tests run on Cloud
        handler.setFormatter(MonologFormatter(channel='c' * 300_000))
        for name, msg in (('n' * 300_000, 'm'), ('app', 'x' * 262_100), ('app', 'y' * 262_200)):
            handler.handle(logging.LogRecord(name, logging.ERROR, __file__, 1, msg, (), None))
        sys.__stdout__.flush()
        raw = sys.__stdout__.buffer.getvalue()
        entries = stdout()
    assert len(entries) == 3
    assert all(list(e) == KEYS and e['level_name'] == 'ERROR' for e in entries)
    assert all(len(line) + 1 <= 256 * 1024 for line in raw.split(b'\n') if line)

    # Every byte of the three free-form fields becomes a six-byte \u0000 escape.
    nul = '\x00' * 20_000
    line = MonologFormatter(channel=nul).format(logging.LogRecord(nul, logging.ERROR, '', 1, nul, (), None))
    assert len(line.encode()) + 1 <= 256 * 1024
    assert json.loads(line)['level_name'] == 'ERROR'


@pytest.mark.parametrize('char', ['a', 'é', '雪', '😀', '\x00', '"', '\\', 'a😀\x00', '\ud800'])
def test_exact_size_boundaries_never_exceed_the_cap(char):
    S, L = lcl._STRING, lcl._LINE

    def check(formatter, record, **extra):
        record.__dict__.update(extra)
        line = formatter.format(record)
        assert len(line.encode(errors='backslashreplace')) + 1 <= L
        entry = json.loads(line)
        assert list(entry) == KEYS
        assert entry['level_name'] == 'ERROR'
        for text in (entry['message'], entry['channel'], entry['extra']['logger']):
            assert '\ufffd' not in text  # never cut mid-character
        return entry

    def record(name='app', msg='m', exc=None):
        return logging.LogRecord(name, logging.ERROR, '', 1, msg, (), exc)

    plain = MonologFormatter(channel='c')
    # At, one under and one over every limit the cut and the ladder use, in every free-form field.
    for limit in sorted({S // 4, S // 2, S, L // 6, L // 4, L}):
        for size in (limit - 1, limit, limit + 1):
            text = (char * size)[:size]
            error = ValueError(text)
            check(plain, record(msg=text))
            check(plain, record(), value=text, key={text: 1})
            check(plain, record(exc=(ValueError, error, None)))
            check(MonologFormatter(channel=text), record(name=text, msg=text, exc=(ValueError, error, None)))

    # A message sized so the whole line is a few bytes under, at, or over the cap.
    width = len(json.dumps(char, ensure_ascii=False).encode(errors='backslashreplace')) - 2
    base = len(plain.format(record(msg='')).encode()) + 1
    for target in range(L - 3 * width, L + 3 * width + 1):
        count = (target - base) // width
        entry = check(plain, record(msg=char * count))
        if base + count * width <= L:
            assert entry['message'] == char * count, 'cut a line that fit'

    # Enough large fields that every step of the fallback ladder runs.
    for count in (15, 16, 17, 40):
        big = char * 20_000
        fields = {f'k{i}': char * (S + 1) for i in range(count)}
        exc = (ValueError, ValueError(big), None)
        check(MonologFormatter(channel=big), record(name=big, msg=big, exc=exc), **fields)


def test_lone_surrogates_are_escaped_not_dropped():
    with captured_stdout() as stdout:
        handler = CloudHandler(None)
        handler.address = None  # stdout, even if the tests run on Cloud
        handler.setFormatter(MonologFormatter(channel='\udcff'))
        record = logging.LogRecord('\udcfe', logging.ERROR, __file__, 1, 'file \udcfd', (), None)
        record.path = '/tmp/\udcfc'
        handler.handle(record)
        sys.__stdout__.flush()
        (entry,) = stdout()
    assert entry['message'] == 'file \udcfd'
    assert entry['channel'] == '\udcff'
    assert entry['extra']['logger'] == '\udcfe'
    assert entry['context']['path'] == '/tmp/\udcfc'


def test_handler_failures_never_raise():
    handler = CloudHandler(None)
    handler.setFormatter(logging.Formatter('%(message)s'))  # a formatter that raises
    with patch.object(sys, 'stderr', open(os.devnull, 'w')) as closed:
        closed.close()
        handler.handle(Broken('x', logging.ERROR, __file__, 1, 'm', (), None))

    class FailingSocket:
        def sendall(self, data):
            raise OSError('send failed')

        def close(self):
            raise OSError('close failed')

    handler = CloudHandler('unix:///unused')
    handler.setFormatter(MonologFormatter())
    handler.sock, handler.pid = FailingSocket(), os.getpid()
    with captured_stdout() as stdout:
        handler.handle(logging.LogRecord('x', logging.ERROR, __file__, 1, 'still here', (), None))
        sys.__stdout__.flush()
        assert stdout()[0]['message'] == 'still here'
    assert handler.sock is None


def test_reconnects_after_retry_delay(collector):
    handler = CloudHandler(collector.address)
    handler.setFormatter(MonologFormatter())

    def record(msg):
        return logging.LogRecord('x', logging.INFO, __file__, 1, msg, (), None)

    clock = [1000.0]
    with patch('laravel_cloud_logging.time.monotonic', lambda: clock[0]), captured_stdout() as stdout:
        handler.retry_at = 1004.0  # as if a failure happened 1 s ago
        handler.handle(record('waiting'))
        assert lcl.flush(5)
        assert handler.sock is None
        clock[0] = 1005.0
        handler.handle(record('reconnected'))
        sys.__stdout__.flush()
        assert [e['message'] for e in stdout()] == ['waiting']
    assert collector.find('reconnected')
    assert handler.sock.gettimeout() == 2.0
    handler.close()


def record(msg, level=logging.INFO):
    return logging.LogRecord('app', level, __file__, 1, msg, (), None)


def stdout_handler():
    handler = CloudHandler(None)
    handler.address = None  # stdout, even if the tests run on Cloud
    handler.setFormatter(MonologFormatter())
    return handler


def test_emit_never_blocks_on_a_socket_that_stopped_reading():
    server = socket.create_server(('127.0.0.1', 0))  # never accepts or reads: the kernel buffers fill up
    handler = CloudHandler(f'tcp://127.0.0.1:{server.getsockname()[1]}')
    handler.setFormatter(MonologFormatter())
    with captured_stdout() as stdout:
        slowest = 0.0
        for _ in range(200):
            start = time.perf_counter()
            handler.handle(record('x' * 65536))  # 13 MB in all, far past the socket buffers
            slowest = max(slowest, time.perf_counter() - start)
        assert slowest < 0.1  # a synchronous send blocks for the 2 s socket timeout
        assert lcl.flush(0.2) is False  # the writer is stuck in sendall
        server.close()
        handler.close()  # gives up after its deadline; the queue empties into stdout
        assert lcl.flush(10)
        assert stdout()[-1]['message'] == 'x' * 65536


def test_full_queue_drops_lines_and_reports_the_count_once_it_drains(monkeypatch):
    monkeypatch.setenv('LARAVEL_CLOUD_LOG_QUEUE', '2')
    entered, release, write = threading.Event(), threading.Event(), lcl._stdout

    def blocked(data):
        entered.set()
        release.wait(5)
        write(data)

    with captured_stdout() as stdout, patch.object(lcl, '_stdout', blocked):
        handler = stdout_handler()
        handler.handle(record('first'))
        assert entered.wait(5)  # the writer holds 'first'; two more fit in the queue
        for msg in ('second', 'third', 'lost', 'also lost'):
            handler.handle(record(msg))
        release.set()
        assert lcl.flush(5)
        handler.handle(record('after'))
        entries = stdout()
    assert [e['message'] for e in entries] == [
        'first',
        'second',
        'third',
        'Dropped 2 log lines: the log queue was full',
        'after',
    ]
    assert entries[3]['level_name'] == 'WARNING'
    assert entries[3]['context'] == {'dropped': 2}
    assert list(entries[3]) == KEYS


@pytest.mark.parametrize(('value', 'size'), [(None, 10_000), ('50', 50), ('0', 1), ('-3', 1), ('lots', 10_000)])
def test_queue_capacity_comes_from_the_environment(monkeypatch, value, size):
    if value is None:
        monkeypatch.delenv('LARAVEL_CLOUD_LOG_QUEUE', raising=False)
    else:
        monkeypatch.setenv('LARAVEL_CLOUD_LOG_QUEUE', value)
    assert CloudHandler(None).queue.maxsize == size


def test_sync_mode_writes_on_the_callers_thread(monkeypatch, collector):
    monkeypatch.setenv('LARAVEL_CLOUD_LOG_SYNC', '1')
    handler = CloudHandler(collector.address)
    handler.setFormatter(MonologFormatter())
    handler.handle(record('sync'))
    assert handler.writer is None
    assert handler.sock is not None
    assert collector.find('sync')
    handler.close()
    assert handler.sock is None


def test_writer_closes_the_socket_when_the_handler_closes(collector):
    handler = CloudHandler(collector.address)
    handler.setFormatter(MonologFormatter())
    handler.handle(record('queued'))
    writer = handler.writer
    handler.close()
    writer.join(5)
    assert not writer.is_alive()
    assert handler.sock is None
    assert collector.find('queued')


def test_after_close_or_during_shutdown_lines_go_to_stdout_right_away(collector):
    handler = CloudHandler(collector.address)
    handler.setFormatter(MonologFormatter())
    with captured_stdout() as stdout:
        handler.close()
        handler.handle(record('after close'))
        assert handler.writer is None
        handler.closed = False
        with patch.object(sys, 'is_finalizing', lambda: True):
            handler.handle(record('finalizing'))
        assert handler.writer is None
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError("can't create new thread")):
            handler.handle(record('no threads'))
        assert handler.writer is None
        assert [e['message'] for e in stdout()] == ['after close', 'finalizing', 'no threads']
    assert collector.lines == []


def test_a_forked_handler_starts_its_own_writer_and_queue():
    with captured_stdout() as stdout:
        handler = stdout_handler()
        handler.handle(record('parent'))
        assert lcl.flush(5)
        parent_queue, parent_writer = handler.queue, handler.writer
        handler.pid, handler.queued = -1, 5  # as if forked with lines the parent still had to write
        assert lcl.flush(0)  # the child doesn't wait for the parent's lines
        handler.handle(record('child'))
        assert handler.pid == os.getpid()
        assert handler.queue is not parent_queue
        assert handler.writer is not parent_writer
        assert [e['message'] for e in stdout()] == ['parent', 'child']
        handler.pid = -1
        handler.close()  # a child closing its inherited handler leaves the parent's writer alone
        assert handler.writer is None
        assert parent_writer.is_alive()


def test_flush_never_raises():
    with patch.object(lcl, '_handlers', [object()]):
        assert lcl.flush(1) is False


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='needs fork')
def test_fork_while_another_thread_holds_the_handler_lock(collector):
    holding, release = threading.Event(), threading.Event()

    class Holding(MonologFormatter):
        def format(self, record):
            if record.msg == 'hold':  # handle() holds the handler lock while it formats
                holding.set()
                release.wait(10)
            return super().format(record)

    handler = CloudHandler(collector.address)
    handler.setFormatter(Holding())
    handler.handle(record('parent'))  # a running writer thread, whose queue lock the child must not use
    assert lcl.flush(5)
    thread = threading.Thread(target=handler.handle, args=(record('hold'),))
    thread.start()
    assert holding.wait(5)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)  # 3.12+: fork with threads
        pid = os.fork()
    if pid == 0:  # logging reinitialises handler locks in the child, so handle() can't block forever
        handler.handle(record('from child'))
        os._exit(0 if lcl.flush(5) else 1)
    try:
        for _ in range(500):
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.01)
        else:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail('child blocked on the handler lock')
        assert os.waitstatus_to_exitcode(status) == 0
        assert collector.find('from child')
    finally:
        release.set()
        thread.join(5)
        handler.close()
