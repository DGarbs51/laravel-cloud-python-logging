def header_id(value: object) -> str | None:
    """Return a Cloud-Request-ID header value, or None when it is missing or invalid."""
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None
