"""Service layer for POST /v1/audio/speech.

Orchestrates: Bearer extraction → IAuthProvider.validate → validation →
normalization → engine.synthesize → encoder wrapping → StreamingResponse.
"""

from __future__ import annotations

import re
from typing import Optional


_BEARER_RE = re.compile(r"^Bearer\s+(.+)$", re.IGNORECASE)


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Extract the token from an `Authorization: Bearer <token>` header.

    Returns the trimmed token if the scheme is `Bearer` (case-insensitive)
    and a non-empty token follows. Returns None in any other case:
    missing header, empty string, no scheme, wrong scheme, no token,
    or whitespace-only token.

    >>> extract_bearer_token("Bearer abc")
    'abc'
    >>> extract_bearer_token("bearer ABC")
    'ABC'
    >>> extract_bearer_token(None) is None
    True
    >>> extract_bearer_token("Basic abc") is None
    True
    >>> extract_bearer_token("Bearer ") is None
    True
    """
    if not authorization:
        return None
    m = _BEARER_RE.match(authorization.strip())
    if not m:
        return None
    token = m.group(1).strip()
    return token if token else None