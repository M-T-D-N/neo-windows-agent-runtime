"""Shared metadata checks; these never adopt tabs or authorize browser changes."""
from urllib.parse import parse_qsl, urlsplit

_CREDENTIAL_KEYS = frozenset(('token', 'access_token', 'id_token', 'code', 'password', 'secret'))


def credential_bearing_url(value):
    url = urlsplit(value)
    return bool(url.username or url.password or
                any(key.lower() in _CREDENTIAL_KEYS for key, _ in parse_qsl(url.query)))


def other_owner(page):
    return (page.get('ownership') not in ('mine', 'user') or
            (page.get('ownership') == 'user' and page.get('ownerAgentId') is not None))
