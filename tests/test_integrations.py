"""Each documented framework recipe, run against the real framework."""

import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from helpers import lines_after

import laravel_cloud_logging as lcl
from laravel_cloud_logging import CloudHandler, configure


def only_cloud_handler():
    root = logging.getLogger()
    return len(root.handlers) == 1 and isinstance(root.handlers[0], CloudHandler)


def test_flask():
    flask = pytest.importorskip('flask')

    def run():
        configure(exceptions=False)
        app = flask.Flask(__name__)
        app.wsgi_app = lcl.wsgi_middleware(app.wsgi_app)

        @app.get('/')
        def index():
            app.logger.info('flask view')
            return 'ok'

        assert app.test_client().get('/', headers={'Cloud-Request-ID': 'f-1'}).text == 'ok'

    entry = lines_after(run)[-1]
    assert entry['message'] == 'flask view'
    assert entry['context'] == {'cloud_request_id': 'f-1'}


def test_starlette_and_uvicorn_config():
    pytest.importorskip('starlette')
    uvicorn = pytest.importorskip('uvicorn')
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    def run():
        configure(exceptions=False)

        def index(request):
            logging.getLogger('app').info('starlette view')
            return PlainTextResponse('ok')

        app = Starlette(routes=[Route('/', index)])
        app.add_middleware(lcl.asgi_middleware)
        with TestClient(app) as client:
            assert client.get('/', headers={'Cloud-Request-ID': 's-1'}).text == 'ok'
        uvicorn.Config(app, log_config=None)  # what uvicorn.run(..., log_config=None) builds
        assert only_cloud_handler()

    entry = next(e for e in lines_after(run) if e['message'] == 'starlette view')
    assert entry['context'] == {'cloud_request_id': 's-1'}


def test_django_middleware():
    pytest.importorskip('django')
    from django.conf import settings
    from django.test import RequestFactory

    from laravel_cloud_logging.django import middleware

    if not settings.configured:
        settings.configure(LOGGING_CONFIG=None, ALLOWED_HOSTS=['*'])
        import django

        django.setup()
    seen = []
    handler = middleware(lambda request: seen.append(lcl.cloud_request_id.get()) or 'response')
    factory = RequestFactory()
    assert handler(factory.get('/', HTTP_CLOUD_REQUEST_ID='d-1')) == 'response'
    handler(factory.get('/'))
    assert seen == ['d-1', None]

    # Django's setup() runs configure_logging(LOGGING_CONFIG, LOGGING); with None it must leave ours alone.
    from django.utils.log import configure_logging

    configure(exceptions=False)
    configure_logging(settings.LOGGING_CONFIG, settings.LOGGING)
    assert only_cloud_handler()
    assert logging.getLogger('django').handlers == []


def test_gunicorn_logconfig_dict():
    pytest.importorskip('gunicorn')
    from gunicorn.config import Config
    from gunicorn.glogging import Logger

    cfg = Config()
    cfg.set('logconfig_dict', configure(exceptions=False))
    Logger(cfg)
    assert only_cloud_handler()
    error = logging.getLogger('gunicorn.error')
    assert error.handlers == []
    assert error.propagate
    assert logging.getLogger('gunicorn.access').propagate is False

    entries = lines_after(lambda: (Logger(cfg), error.info('Booting worker')))
    assert entries[-1]['message'] == 'Booting worker'
    assert entries[-1]['extra'] == {'logger': 'gunicorn.error'}


def test_celery_setup():
    celery = pytest.importorskip('celery')
    from celery.signals import setup_logging

    from laravel_cloud_logging.celery import setup

    app = celery.Celery('test')
    setup(app, level='DEBUG', exceptions=False)
    assert app.conf.worker_hijack_root_logger is False
    logging.getLogger().handlers.clear()
    setup_logging.send(sender=None, loglevel='INFO', logfile=None, format='', colorize=False)
    assert only_cloud_handler()
    assert logging.getLogger().level == logging.DEBUG


def test_rq_either_order():
    pytest.importorskip('rq')
    from rq.logutils import setup_loghandlers

    def run():
        configure(exceptions=False)
        setup_loghandlers('INFO')  # configure() first: rq sees root's handler and adds none
        logging.getLogger('rq.worker').info('job started')

    entries = lines_after(run)
    assert [e['message'] for e in entries] == ['job started']

    logging.getLogger().handlers.clear()
    setup_loghandlers('INFO')  # rq first: configure() removes rq's stdout handlers
    configure(exceptions=False)
    assert logging.getLogger('rq.worker').handlers == []


def test_hypercorn_logconfig_dict():
    pytest.importorskip('hypercorn')
    from hypercorn.config import Config
    from hypercorn.logging import Logger

    cfg = Config()
    cfg.accesslog = '-'  # even when enabled, the access logger stays silent
    cfg.logconfig_dict = configure(exceptions=False)
    Logger(cfg)  # installs its own stderr/stdout handlers, then applies logconfig_dict
    assert only_cloud_handler()
    assert logging.getLogger('hypercorn.error').handlers == []
    assert logging.getLogger('hypercorn.error').propagate
    access = logging.getLogger('hypercorn.access')
    assert access.handlers == []
    assert access.propagate is False


def test_granian_worker_then_configure():
    pytest.importorskip('granian')
    from granian.log import LogLevels, configure_logging

    def run():
        configure_logging(LogLevels.info)  # what each worker runs before it imports the app
        configure(exceptions=False)  # the app module's import
        logging.getLogger('_granian').info('Started worker-1')

    entries = lines_after(run)
    assert [e['message'] for e in entries] == ['Started worker-1']
    assert only_cloud_handler()
    assert logging.getLogger('granian.access').propagate is False


def test_waitress_serve_level():
    pytest.importorskip('waitress')

    logging.getLogger('waitress').setLevel(logging.INFO)  # waitress-serve, before it imports the app
    configure(level='WARNING', exceptions=False)  # the app module's import
    logging.basicConfig()  # waitress.serve(); a no-op once root has a handler
    assert only_cloud_handler()
    assert logging.getLogger('waitress').level == logging.WARNING


ASGI_APP = """
from laravel_cloud_logging import asgi_middleware, configure

configure()


async def _app(scope, receive, send):
    if scope['type'] != 'http':
        return
    if scope['path'] == '/boom':
        raise RuntimeError('boom')
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': b'ok'})


app = asgi_middleware(_app)
"""
WSGI_APP = """
from laravel_cloud_logging import configure, wsgi_middleware

configure()


def _app(environ, start_response):
    if environ['PATH_INFO'] == '/boom':
        raise RuntimeError('boom')
    start_response('200 OK', [])
    return [b'ok']


app = wsgi_middleware(_app)
"""
# Each documented start command, with {port} filled in. uWSGI closes the connection on an uncaught exception.
SERVERS = {
    'uvicorn': (ASGI_APP, ['--port', '{port}', '--workers', '2', '--log-config', 'logging.json', 'app:app'], 500),
    'hypercorn': (
        ASGI_APP,
        ['--bind', '127.0.0.1:{port}', '--workers', '2', '--log-config', 'json:logging.json', 'app:app'],
        500,
    ),
    'granian': (
        WSGI_APP,
        ['--port', '{port}', '--interface', 'wsgi', '--workers', '2', '--log-config', 'logging.json', 'app:app'],
        500,
    ),
    'gunicorn': (WSGI_APP, ['-b', '127.0.0.1:{port}', 'app:app'], 500),
    'waitress': (WSGI_APP, ['--listen', '127.0.0.1:{port}', 'app:app'], 500),
    'daphne': (ASGI_APP, ['-v', '0', '-p', '{port}', 'app:app'], 500),
    'uwsgi': (
        WSGI_APP,
        [
            *('--http-socket', '127.0.0.1:{port}', '--module', 'app:app', '--master', '--processes', '2'),
            *('--enable-threads', '--need-app', '--disable-logging', '--die-on-term'),
        ],
        None,
    ),
}


def serve(tmp_path, server, path='/'):
    """Boot a server on the documented command, GET path with a Cloud-Request-ID, SIGTERM; (status, records)."""
    source, args, _ = SERVERS[server]
    (tmp_path / 'app.py').write_text(source)
    env = {**os.environ, 'PYTHONPATH': str(tmp_path), 'PYTHONUNBUFFERED': '1'}
    env.pop('LARAVEL_CLOUD', None)
    subprocess.run(
        [sys.executable, '-m', 'laravel_cloud_logging.config', 'logging.json'], cwd=tmp_path, env=env, check=True
    )
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    # pyuwsgi only ships a console script, next to this interpreter.
    launcher = [str(Path(sys.executable).with_name('uwsgi'))] if server == 'uwsgi' else [sys.executable, '-m', server]
    command = [*launcher, *(a.format(port=port) for a in args)]
    proc = subprocess.Popen(command, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    status = None
    for _ in range(300):
        with contextlib.suppress(OSError):  # not listening yet
            urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5).read()
            break
        time.sleep(0.1)
    with contextlib.suppress(OSError):  # uWSGI drops the connection on an uncaught exception
        request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers={'Cloud-Request-ID': 'req-1'})
        try:
            status = urllib.request.urlopen(request, timeout=10).status
        except urllib.error.HTTPError as error:
            status = error.code
    time.sleep(0.5)  # let the worker finish logging before shutdown
    proc.send_signal(signal.SIGTERM)
    try:
        output = proc.communicate(timeout=30)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        output = proc.communicate()[0]
        pytest.fail(f'server did not stop:\n{output}')
    return status, output


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='multi-worker servers need fork')
@pytest.mark.parametrize('server', ['uvicorn', 'hypercorn', 'granian'])
def test_server_main_process_logs_json_with_log_config_file(tmp_path, server):
    pytest.importorskip(server)
    status, output = serve(tmp_path, server)
    assert status == 200, output
    lines = output.splitlines()
    assert [line for line in lines if not line.startswith('{')] == []
    entries = [json.loads(line) for line in lines]
    assert entries
    if server == 'granian':  # its thread-count warning keeps its level
        assert any(e['level_name'] == 'WARNING' and e['extra'] == {'logger': '_granian'} for e in entries)


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='multi-worker servers need fork')
@pytest.mark.parametrize('server', list(SERVERS))
def test_uncaught_request_exception_is_one_structured_record_with_the_request_id(tmp_path, server):
    pytest.importorskip('pyuwsgi' if server == 'uwsgi' else server)
    status, output = serve(tmp_path, server, '/boom')
    assert status == SERVERS[server][2], output
    records = [json.loads(line) for line in output.splitlines() if line.startswith('{')]
    errors = [r for r in records if 'boom' in json.dumps(r)]
    assert len(errors) == 1, output
    assert errors[0]['message'] == 'Uncaught exception in GET /boom'
    assert errors[0]['level_name'] == 'ERROR'
    assert errors[0]['context']['cloud_request_id'] == 'req-1'
    assert errors[0]['context']['exception']['class'] == 'RuntimeError'
