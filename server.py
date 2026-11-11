"""Minimal FastMCP server secured by Microsoft Entra ID that calls Microsoft Graph via OBO.

Flow:
1. The MCP client signs in through FastMCP's OAuth proxy (AzureProvider). Entra issues a
   token whose audience is THIS server's API (api://{client_id}, scope `mcp-access`).
2. Every request to /mcp is validated against that audience and scope.
3. A tool that needs Graph asks for `EntraOBOToken([...])`: FastMCP exchanges the user's
   token for a Graph token On-Behalf-Of the user. The client never sees a Graph token.
4. Calling your own API works the same way: OBO for `api://{api_client_id}/Data.Read`, then send
   that token to the API (downstream_api.py), which validates it was issued for itself.
"""

import logging
import os
from collections.abc import Mapping

import httpx2
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentAccessToken
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.azure import AzureProvider, EntraOBOToken

GRAPH_USER_READ = "https://graph.microsoft.com/User.Read"
REQUIRED = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
log = logging.getLogger(__name__)


def load_settings(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Read config from the environment; fail fast naming any missing variable."""
    missing = [key for key in REQUIRED if not env.get(key)]
    if missing:
        raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}")
    return {key: env[key] for key in REQUIRED} | {
        "BASE_URL": env.get("BASE_URL", "http://localhost:8000"),
        # Optional: your own downstream API (downstream_api.py). Unset = Graph tools only.
        "API_CLIENT_ID": env.get("API_CLIENT_ID", ""),
        "DOWNSTREAM_API_URL": env.get("DOWNSTREAM_API_URL", "").rstrip("/"),
        "API_SCOPE": env.get("API_SCOPE", "Data.Read"),  # same key downstream_api.py checks
    }


def downstream_scope(settings: Mapping[str, str]) -> str | None:
    """The downstream API's delegated scope, or None when that API isn't configured."""
    if settings["API_CLIENT_ID"] and settings["DOWNSTREAM_API_URL"]:
        return f"api://{settings['API_CLIENT_ID']}/{settings['API_SCOPE']}"
    return None


def create_auth(settings: Mapping[str, str]) -> AzureProvider:
    obo_scopes = [GRAPH_USER_READ]
    if scope := downstream_scope(settings):
        obo_scopes.append(scope)
    return AzureProvider(
        tenant_id=settings["AZURE_TENANT_ID"],
        client_id=settings["AZURE_CLIENT_ID"],
        client_secret=settings["AZURE_CLIENT_SECRET"],
        base_url=settings["BASE_URL"],
        # Tokens must have audience api://{client_id} and scp `mcp-access`.
        required_scopes=["mcp-access"],
        # Requested at sign-in (so the user consents once) but never sent to the client.
        # Scopes passed to EntraOBOToken must be a subset of these. Several resources are fine
        # here; FastMCP redeems the sign-in code for this server's own scope only.
        # The API scope is listed too so user consent and the subset rule hold even without
        # pre-authorization. With pre-authorization (what the script sets up) it is probably
        # not needed at sign-in; that is unverified against a live tenant (issue #1).
        additional_authorize_scopes=obo_scopes,
    )


def check(response: httpx2.Response, service: str) -> None:
    """On non-2xx raise a generic ToolError; the body may hold internal detail, so only log it."""
    if response.is_error:
        log.warning("%s returned %s: %s", service, response.status_code, response.text[:300])
        raise ToolError(f"{service} request failed ({response.status_code})")


load_dotenv()  # optional .env; real environment variables win
settings = load_settings()
DOWNSTREAM_SCOPE = downstream_scope(settings)
mcp = FastMCP("entra-obo-example", auth=create_auth(settings))


@mcp.tool
async def whoami(token: AccessToken = CurrentAccessToken()) -> dict:
    """Return identity claims from the validated incoming token (no Graph call)."""
    keys = ("name", "preferred_username", "oid", "tid", "scp")
    return {key: token.claims.get(key) for key in keys}


@mcp.tool
async def get_me(graph_token: str = EntraOBOToken([GRAPH_USER_READ])) -> dict:
    """Return the signed-in user's Microsoft Graph profile, fetched via OBO."""
    async with httpx2.AsyncClient(timeout=10) as client:
        response = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            params={"$select": "displayName,mail,userPrincipalName,id"},
            headers={"Authorization": f"Bearer {graph_token}"},
        )
    check(response, "Microsoft Graph")
    return response.json()


if DOWNSTREAM_SCOPE:  # register the tool only when the downstream API is configured

    @mcp.tool
    async def get_downstream_profile(api_token: str = EntraOBOToken([DOWNSTREAM_SCOPE])) -> dict:
        """Call your own downstream API with an OBO token issued for it (not for this server)."""
        async with httpx2.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{settings['DOWNSTREAM_API_URL']}/api/profile",
                headers={"Authorization": f"Bearer {api_token}"},
            )
        check(response, "Downstream API")
        return response.json()


if __name__ == "__main__":
    # HOST: loopback by default; the container sets 0.0.0.0 so the published port reaches it.
    mcp.run(transport="http", host=os.environ.get("HOST", "127.0.0.1"), port=8000)
