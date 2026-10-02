"""Each documented framework recipe, run against the real framework."""

import logging
import os
import sys
from unittest.mock import patch

import pytest

import laravel_cloud_logging as lcl
from laravel_cloud_logging import CloudHandler, configure
from helpers import captured_stdout


def lines_after(action):
    with patch.dict(os.environ, {'LARAVEL_CLOUD': ''}), captured_stdout() as stdout:
        action()
        sys.__stdout__.flush()
        return stdout()


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
    assert entry['message'] == 'flask view' and entry['context'] == {'cloud_request_id': 'f-1'}


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

    entry = [e for e in lines_after(run) if e['message'] == 'starlette view'][0]
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

    configure(exceptions=False)
    assert only_cloud_handler()  # LOGGING_CONFIG = None: django.setup() left logging alone


def test_gunicorn_logconfig_dict():
    pytest.importorskip('gunicorn')
    from gunicorn.config import Config
    from gunicorn.glogging import Logger

    cfg = Config()
    cfg.set('logconfig_dict', configure(exceptions=False))
    Logger(cfg)
    assert only_cloud_handler()
    error = logging.getLogger('gunicorn.error')
    assert error.handlers == [] and error.propagate
    assert logging.getLogger('gunicorn.access').propagate is False

    entries = lines_after(lambda: (Logger(cfg), error.info('Booting worker')))
    assert entries[-1]['message'] == 'Booting worker' and entries[-1]['extra'] == {'logger': 'gunicorn.error'}


def test_celery_setup():
    celery = pytest.importorskip('celery')
    from celery.signals import setup_logging

    from laravel_cloud_logging.celery import setup

    app = celery.Celery('test')
    setup(app, level='DEBUG', exceptions=False)
    assert app.conf.worker_hijack_root_logger is False
    logging.getLogger().handlers.clear()
    setup_logging.send(sender=None, loglevel='INFO', logfile=None, format='', colorize=False)
    assert only_cloud_handler() and logging.getLogger().level == logging.DEBUG


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
