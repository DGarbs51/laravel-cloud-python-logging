"""Django: in settings.py set LOGGING_CONFIG = None and call configure(), then add
'laravel_cloud_logging.django.middleware' near the top of MIDDLEWARE.

Sync-only, so Django adapts it under ASGI. For ASGI deployments, wrapping the
application with asgi_middleware() in asgi.py avoids that adapter.
"""

from . import _header_id, cloud_request_id


def middleware(get_response):
    def wrapped(request):
        # Set on every request (None when absent), so a reused thread never keeps a stale ID.
        cloud_request_id.set(_header_id(request.META.get('HTTP_CLOUD_REQUEST_ID')))
        return get_response(request)
    return wrapped
