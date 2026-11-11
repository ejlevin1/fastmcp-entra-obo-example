import functools

import httpx2
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken

import server

pytestmark = pytest.mark.anyio

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
API_CLIENT_ID = "22222222-2222-2222-2222-222222222222"


def test_provider_scopes():
    auth = server.create_auth(server.load_settings())
    assert auth.identifier_uri == f"api://{CLIENT_ID}"  # token audience
    assert auth.required_scopes == ["mcp-access"]  # checked against the `scp` claim
    # Every EntraOBOToken scope (get_me, get_downstream_profile) must be a subset of these.
    assert server.DOWNSTREAM_SCOPE == f"api://{API_CLIENT_ID}/Data.Read"
    obo_scopes = {server.GRAPH_USER_READ, server.DOWNSTREAM_SCOPE}
    assert obo_scopes <= set(auth.additional_authorize_scopes)


def test_downstream_api_is_optional():
    env = {"AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s"}
    settings = server.load_settings(env)
    assert server.downstream_scope(settings) is None
    assert not any("api://" in s for s in server.create_auth(settings).additional_authorize_scopes)


def test_downstream_scope_follows_api_scope():
    # One API_SCOPE setting drives both the OBO request and the API's `scp` check.
    env = {"AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s"}
    env |= {"API_CLIENT_ID": "a", "DOWNSTREAM_API_URL": "http://api", "API_SCOPE": "Data.Write"}
    assert server.downstream_scope(server.load_settings(env)) == "api://a/Data.Write"


def test_missing_env_fails_fast():
    with pytest.raises(SystemExit, match="AZURE_CLIENT_SECRET"):
        server.load_settings({"AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c"})


def test_base_url_default():
    env = {"AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s"}
    assert server.load_settings(env)["BASE_URL"] == "http://localhost:8000"


async def test_whoami_returns_claims():
    claims = {"name": "Test User", "preferred_username": "user@example.com", "oid": "o"}
    claims |= {"tid": "t", "scp": "mcp-access", "aud": "ignored"}
    token = AccessToken(token="x", client_id="c", scopes=["mcp-access"], claims=claims)
    result = await server.whoami(token)
    assert result == {k: v for k, v in claims.items() if k != "aud"}


def mock_http(monkeypatch, status, body):
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["request"] = request
        return httpx2.Response(status, json=body)

    transport = httpx2.MockTransport(handler)
    monkeypatch.setattr(
        server.httpx2, "AsyncClient", functools.partial(httpx2.AsyncClient, transport=transport)
    )
    return seen


async def test_get_me_happy_path(monkeypatch):
    profile = {"displayName": "Test User", "mail": None, "userPrincipalName": "u", "id": "1"}
    seen = mock_http(monkeypatch, 200, profile)
    assert await server.get_me("graph-token") == profile
    request = seen["request"]
    assert request.url.path == "/v1.0/me"
    assert request.url.params["$select"] == "displayName,mail,userPrincipalName,id"
    assert request.headers["Authorization"] == "Bearer graph-token"


async def test_get_me_error_is_generic(monkeypatch, caplog):
    mock_http(monkeypatch, 403, {"error": {"code": "Authorization_RequestDenied"}})
    with pytest.raises(ToolError) as excinfo:
        await server.get_me("graph-token")
    # The client sees status + a generic message; the body is only in the server log.
    assert str(excinfo.value) == "Microsoft Graph request failed (403)"
    assert "Authorization_RequestDenied" in caplog.text
    assert "graph-token" not in caplog.text


async def test_get_downstream_profile_sends_obo_token(monkeypatch):
    body = {"oid": "o", "azp": CLIENT_ID, "scp": "Data.Read"}
    seen = mock_http(monkeypatch, 200, body)
    assert await server.get_downstream_profile("api-token") == body
    request = seen["request"]
    assert str(request.url) == "http://localhost:8001/api/profile"
    assert request.headers["Authorization"] == "Bearer api-token"


async def test_get_downstream_profile_error_is_generic(monkeypatch, caplog):
    mock_http(monkeypatch, 401, {"error": "invalid_token"})
    with pytest.raises(ToolError) as excinfo:
        await server.get_downstream_profile("api-token")
    assert str(excinfo.value) == "Downstream API request failed (401)"
    assert "invalid_token" in caplog.text and "api-token" not in caplog.text


def test_env_example_is_graph_only():
    """Copying .env.example must give the Graph-only setup: the optional API keys stay commented."""
    from pathlib import Path

    from dotenv import dotenv_values

    env = dotenv_values(Path(__file__).parent.parent / ".env.example")
    settings = server.load_settings(env)
    assert settings["API_CLIENT_ID"] == "" and settings["DOWNSTREAM_API_URL"] == ""
    assert server.downstream_scope(settings) is None
