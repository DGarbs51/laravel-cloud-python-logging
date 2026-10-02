import asyncio
import json
import logging
import os
import socket
import sys
import threading
import warnings
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from helpers import KEYS, Broken, Collector, captured_stdout, fmt

import laravel_cloud_logging as lcl
from laravel_cloud_logging import CloudHandler, MonologFormatter, configure


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
        when=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
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
        assert config['handlers']['cloud']['()'] is CloudHandler
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
            os._exit(0 if handler.sock is not parent_sock and handler.pid == os.getpid() else 1)
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
        assert handler.sock is None
        clock[0] = 1005.0
        handler.handle(record('reconnected'))
        sys.__stdout__.flush()
        assert [e['message'] for e in stdout()] == ['waiting']
    assert collector.find('reconnected')
    assert handler.sock.gettimeout() == 2.0
    handler.close()
