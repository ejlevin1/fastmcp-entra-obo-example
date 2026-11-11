"""The "underlying application": a tiny API that accepts only Entra tokens issued for itself.

The MCP server calls it with an OBO token, never with the token the MCP client sent (that one's
audience is the MCP server). This API checks the token like any resource server: signature (the
tenant's published keys), issuer, audience = this API, expiry, and the delegated scope in `scp`.
`azp` names the client app that called on the user's behalf: the MCP server.
"""

import logging
import os

import jwt
import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

load_dotenv()  # optional .env; real environment variables win
TENANT_ID = os.environ.get("API_TENANT_ID") or os.environ.get("AZURE_TENANT_ID")
CLIENT_ID = os.environ.get("API_CLIENT_ID")
SCOPE = os.environ.get("API_SCOPE", "Data.Read")
# Optional allow-list of calling apps (`azp`), comma-separated. Unset = any app in the tenant that
# holds a Data.Read token for this API is accepted; set it to the MCP server's client ID.
_allowed = os.environ.get("ALLOWED_CLIENT_IDS", "").split(",")
ALLOWED_CLIENT_IDS = {c.strip() for c in _allowed if c.strip()}
if not (TENANT_ID and CLIENT_ID):
    raise SystemExit("Set API_CLIENT_ID and API_TENANT_ID (or AZURE_TENANT_ID).")

ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"  # v2 tokens only
# Caches the tenant's signing keys; fetches again when a token has an unknown `kid` (rotation).
jwks = jwt.PyJWKClient(f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys")
log = logging.getLogger(__name__)


def reject(status: int, error: str, description: str) -> JSONResponse:
    headers = {"WWW-Authenticate": f'Bearer error="{error}"'}
    return JSONResponse({"error": error, "error_description": description}, status, headers)


# Plain `def`: Starlette runs it in a threadpool, so the blocking JWKS fetch can't stall the loop.
def profile(request: Request) -> JSONResponse:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return JSONResponse({"error": "missing bearer token"}, 401, {"WWW-Authenticate": "Bearer"})
    try:
        key = jwks.get_signing_key_from_jwt(token).key
        # In v2 access tokens `aud` is the API's client ID (a GUID), not its api:// URI.
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer=ISSUER,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        # The reason (e.g. "Invalid audience") goes to the server log only, never the token itself;
        # the caller gets a generic message so it can't probe the validation rules.
        log.warning("Rejected token: %s: %s", type(exc).__name__, exc)
        return reject(401, "invalid_token", "invalid or expired token")
    if SCOPE not in claims.get("scp", "").split():
        return reject(403, "insufficient_scope", f"scope {SCOPE} required")
    if ALLOWED_CLIENT_IDS and claims.get("azp") not in ALLOWED_CLIENT_IDS:
        # The token is valid (so not 401 invalid_token); the calling app just isn't allowed.
        # RFC 6750 has no code for that, so use RFC 6749's `unauthorized_client` ("authenticated
        # but not authorized"), not `invalid_client` (which means client authentication failed).
        return reject(403, "unauthorized_client", "calling application not allowed")
    keys = ("oid", "name", "preferred_username", "scp", "azp")  # azp = the MCP server's client ID
    return JSONResponse({key: claims.get(key) for key in keys})


app = Starlette(routes=[Route("/api/profile", profile)])

if __name__ == "__main__":
    # HOST: loopback by default; the container sets 0.0.0.0 so the published port reaches it.
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=8001)
