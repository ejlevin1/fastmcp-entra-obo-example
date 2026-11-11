"""downstream_api.py with a locally generated RSA key standing in for the tenant's signing key."""

import inspect
import time

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import downstream_api

pytestmark = pytest.mark.anyio

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
MCP_CLIENT_ID = "11111111-1111-1111-1111-111111111111"
VALID = {
    "iss": downstream_api.ISSUER,
    "aud": downstream_api.CLIENT_ID,  # v2 token: the API's client ID
    "exp": int(time.time()) + 600,
    "scp": "Data.Read",
    "azp": MCP_CLIENT_ID,  # the MCP server that did the OBO exchange
    "oid": "o",
    "name": "Test User",
    "preferred_username": "user@example.com",
}


@pytest.fixture(autouse=True)
def fake_jwks(monkeypatch):
    """Serve KEY's public JWK instead of fetching the tenant's keys over the network."""
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True) | {"kid": "k1"}
    monkeypatch.setattr(downstream_api.jwks, "fetch_data", lambda: {"keys": [jwk]})


def make_token(key=KEY, **overrides) -> str:
    claims = {k: v for k, v in (VALID | overrides).items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def test_handler_is_sync_so_jwks_fetch_runs_in_threadpool():
    # PyJWKClient fetches keys with blocking urllib; an async handler would stall the event loop.
    assert not inspect.iscoroutinefunction(downstream_api.profile)


async def get_profile(headers: dict) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=downstream_api.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://api") as client:
        return await client.get("/api/profile", headers=headers)


async def test_valid_token_returns_caller_identity():
    response = await get_profile({"Authorization": f"Bearer {make_token()}"})
    assert response.status_code == 200
    assert response.json() == {
        k: VALID[k] for k in ("oid", "name", "preferred_username", "scp")
    } | {"azp": MCP_CLIENT_ID}


@pytest.mark.parametrize(
    "token, status",
    [
        (None, 401),  # no Authorization header
        ("garbage", 401),
        (make_token(exp=int(time.time()) - 60), 401),
        (make_token(aud=f"api://{downstream_api.CLIENT_ID}"), 401),  # v1-style audience
        (make_token(aud=MCP_CLIENT_ID), 401),  # e.g. the token the MCP client sent
        (make_token(iss="https://sts.windows.net/x/"), 401),  # v1 issuer
        (make_token(key=OTHER_KEY), 401),  # bad signature
        (make_token(exp=None), 401),
        (make_token(scp="User.Read"), 403),
        (make_token(scp=None), 403),
    ],
)
async def test_rejects(token, status):
    response = await get_profile({"Authorization": f"Bearer {token}"} if token else {})
    assert response.status_code == status
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


async def test_invalid_token_detail_is_logged_not_returned(caplog):
    token = make_token(aud=MCP_CLIENT_ID)
    response = await get_profile({"Authorization": f"Bearer {token}"})
    assert response.json()["error_description"] == "invalid or expired token"
    assert "InvalidAudienceError" in caplog.text  # PyJWT's reason, server-side only
    assert token not in caplog.text


@pytest.mark.parametrize(
    "allowed, status",
    [
        ({MCP_CLIENT_ID}, 200),  # azp = the MCP server
        ({"33333333-3333-3333-3333-333333333333"}, 403),  # another app's valid Data.Read token
        (set(), 200),  # ALLOWED_CLIENT_IDS unset: any caller with a valid token
    ],
)
async def test_allowed_client_ids(monkeypatch, allowed, status):
    monkeypatch.setattr(downstream_api, "ALLOWED_CLIENT_IDS", allowed)
    response = await get_profile({"Authorization": f"Bearer {make_token()}"})
    assert response.status_code == status
    if status == 403:
        assert response.json()["error"] == "unauthorized_client"
