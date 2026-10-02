"""Django: in settings.py set LOGGING_CONFIG = None and call configure(), then add
'laravel_cloud_logging.django.middleware' near the top of MIDDLEWARE.

Sync-only, so Django adapts it under ASGI. For ASGI deployments, wrapping the
application with asgi_middleware() in asgi.py avoids that adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from . import cloud_request_id
from ._headers import header_id

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase


def middleware(get_response: Callable[[HttpRequest], HttpResponseBase]) -> Callable[[HttpRequest], HttpResponseBase]:
    def wrapped(request: HttpRequest) -> HttpResponseBase:
        # Set on every request (None when absent), so a reused thread never keeps a stale ID.
        cloud_request_id.set(header_id(request.META.get('HTTP_CLOUD_REQUEST_ID')))
        return get_response(request)

    return wrapped
