"""Zitadel OIDC bearer-token validation for the Pulse collector API.

Mirrors the main platform's JWT verification pattern but fetches JWKS with an
explicit Host header because Zitadel routes by Host. Required env:

    OIDC_ISSUER       e.g. https://auth.opuslogic.eu
    OIDC_JWKS_URI     internal URL to Zitadel's JWKS endpoint
                      e.g. http://opuslogic_zitadel:8080/oauth/v2/keys
    OIDC_JWKS_HOST    Host header to use when fetching JWKS
                      (default: the hostname from OIDC_ISSUER)
    OIDC_PROJECT_ID   Zitadel project-id / audience the tokens are minted for
"""
from __future__ import annotations

import logging
import hmac
import os
import time
from typing import Optional
from urllib.parse import urlparse

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = logging.getLogger("pulse.auth")

ROLE_SCOPES: dict[str, set[str]] = {
    "viewer": {"platform.read"},
    "operator": {"platform.read", "platform.write"},
    "tenant-admin": {"platform.read", "platform.write", "admin.read"},
    "platform-admin": {"platform.read", "platform.write", "admin.read", "admin.write"},
    "compliance-officer": {"platform.read"},
}
ZITADEL_ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"
REQUIRED_SCOPE = "platform.read"

bearer_scheme = HTTPBearer(auto_error=False)

_keys_cache: dict[str, object] = {"ts": 0.0, "by_kid": {}}
_KEYS_TTL = 3600.0


def _jwks_host() -> str:
    explicit = os.environ.get("OIDC_JWKS_HOST")
    if explicit:
        return explicit
    issuer = os.environ.get("OIDC_ISSUER", "")
    return urlparse(issuer).netloc or "auth.opuslogic.eu"


def _fetch_keys() -> dict[str, object]:
    uri = os.environ["OIDC_JWKS_URI"]
    host = _jwks_host()
    log.info("fetching JWKS from %s (Host: %s)", uri, host)
    r = httpx.get(uri, headers={"Host": host}, timeout=5.0)
    r.raise_for_status()
    by_kid: dict[str, object] = {}
    for k in r.json().get("keys", []):
        kid = k.get("kid")
        if not kid:
            continue
        by_kid[kid] = RSAAlgorithm.from_jwk(k)
    return by_kid


def _key_for(kid: str):
    now = time.time()
    cache_by_kid: dict[str, object] = _keys_cache["by_kid"]  # type: ignore[assignment]
    if now - float(_keys_cache["ts"]) > _KEYS_TTL or kid not in cache_by_kid:
        cache_by_kid = _fetch_keys()
        _keys_cache["by_kid"] = cache_by_kid
        _keys_cache["ts"] = now
    if kid not in cache_by_kid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"unknown kid {kid}")
    return cache_by_kid[kid]


def _decode(token: str) -> dict:
    audience = os.environ["OIDC_PROJECT_ID"]
    issuer = os.environ.get("OIDC_ISSUER", "").rstrip("/")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"malformed token: {e}")
    kid = header.get("kid")
    if not kid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token missing kid")
    key = _key_for(kid)
    opts: dict = {"algorithms": ["RS256"], "audience": audience}
    if issuer:
        opts["issuer"] = issuer
    try:
        return jwt.decode(token, key, **opts)
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid token: {e}")


def _scopes_for(claims: dict) -> set[str]:
    roles = claims.get(ZITADEL_ROLES_CLAIM) or {}
    out: set[str] = set()
    for role in roles.keys() if isinstance(roles, dict) else []:
        out.update(ROLE_SCOPES.get(role, set()))
    return out


def _require(claims: dict) -> None:
    if REQUIRED_SCOPE not in _scopes_for(claims):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"missing scope {REQUIRED_SCOPE}",
        )


# R660.2 (OpusLogic Block 220, 2026-09-06). A shared secret that gates the data
# routes WITHOUT Zitadel. The dashboard is reached over an SSH tunnel by one
# person on one machine; on 2026-09-06 nobody on the programme could edit the
# Zitadel application that guards it, so a login that depends on the identity
# provider was the wrong shape for the CEO's own dashboard. Set
# PULSE_SHARED_TOKEN in the environment and present it as the bearer token
# (or ?token= on the WebSocket); the JWT path below is unchanged and keeps
# working for the day the redirect URI lands. Unset, nothing changes.
_SHARED_CLAIMS = {"sub": "pulse-shared-token", ZITADEL_ROLES_CLAIM: {"viewer": {}}}


def _shared_token_matches(token: str) -> bool:
    expected = os.environ.get("PULSE_SHARED_TOKEN", "")
    return bool(expected) and hmac.compare_digest(token.encode(), expected.encode())


async def require_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bearer token required")
    if _shared_token_matches(creds.credentials):
        return dict(_SHARED_CLAIMS)
    claims = _decode(creds.credentials)
    _require(claims)
    return claims


async def verify_token_string(token: str) -> dict:
    if _shared_token_matches(token):
        return dict(_SHARED_CLAIMS)
    claims = _decode(token)
    _require(claims)
    return claims


def auth_required() -> bool:
    return os.environ.get("PULSE_AUTH_DISABLED", "") not in ("1", "true", "yes")
