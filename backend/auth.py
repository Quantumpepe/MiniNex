"""Fail-closed verification of Privy access tokens; identity is NOT wallet authority.

Privy signs app access tokens with ES256. Only a server-configured public
verification key is accepted. No token, invalid signature or missing key may
grant a trading permission. Never log raw JWTs.
"""
from __future__ import annotations

import os

import jwt
from fastapi import HTTPException


def verify_privy_bearer(header: str | None) -> dict[str, str]:
    if not header or not header.startswith("Bearer ") or header.count(" ") != 1:
        raise HTTPException(status_code=401, detail="Privy authentication required")
    token = header[7:]
    if len(token) < 20 or len(token) > 8192:
        raise HTTPException(status_code=401, detail="Invalid access token")

    # Configure this PEM in Render as an environment variable; it must NEVER
    # be included in the published client bundle.
    verification_key = os.environ.get("MININEX_PRIVY_VERIFICATION_KEY", "").replace("\\n", "\n").strip()
    if not verification_key:
        raise HTTPException(status_code=503, detail="Privy verification not configured")

    app_id = "cmw7sb7ty013o0gl55aqyzi3y"
    try:
        claims = jwt.decode(
            token,
            verification_key,
            algorithms=["ES256"],
            audience=app_id,
            issuer="privy.io",
            options={"require": ["sub", "sid", "iss", "aud", "iat", "exp"]},
            leeway=15,
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid Privy authentication") from None
    except (ValueError, TypeError):
        raise HTTPException(status_code=503, detail="Privy verification configuration invalid") from None

    did = claims.get("sub")
    session = claims.get("sid")
    if not isinstance(did, str) or not did.startswith("did:privy:") or not isinstance(session, str) or not session.strip():
        raise HTTPException(status_code=401, detail="Invalid Privy identity")
    return {"user_id": did, "session_id": session}
