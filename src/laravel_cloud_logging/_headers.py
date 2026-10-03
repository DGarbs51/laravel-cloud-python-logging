import re

# Letters, digits and . _ : - only, so an ID can't carry control characters or spaces into any log sink.
_ID = re.compile(r'[A-Za-z0-9._:-]{1,128}')


def header_id(value: object) -> str | None:
    """Return a Cloud-Request-ID header value, or None when it is missing or invalid."""
    return value if isinstance(value, str) and _ID.fullmatch(value) else None
