#!/usr/bin/env bash
# Create (or update) the Entra ID app registrations for this example and write .env:
# the MCP server (APP_NAME) and the downstream API it calls via OBO (API_APP_NAME).
#
#   scripts/create_app_registration.sh            # create or update
#   scripts/create_app_registration.sh --delete   # delete both app registrations
#
# Env overrides: APP_NAME (default fastmcp-entra-obo-example), API_APP_NAME (default
# $APP_NAME-api), BASE_URL (default http://localhost:8000), DOWNSTREAM_API_URL (default
# http://localhost:8001), ENV_FILE (default <repo>/.env), NEW_SECRET=1 to rotate the secret.
# Requires: python3 and the az CLI, logged in (`az login`) with rights to create app registrations.
set -euo pipefail

APP_NAME="${APP_NAME:-fastmcp-entra-obo-example}"
API_APP_NAME="${API_APP_NAME:-$APP_NAME-api}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
DOWNSTREAM_API_URL="${DOWNSTREAM_API_URL:-http://localhost:8001}"
ENV_FILE="${ENV_FILE:-$(cd "$(dirname "$0")/.." && pwd)/.env}"
GRAPH_APP_ID="00000003-0000-0000-c000-000000000000"   # Microsoft Graph
USER_READ_ID="e1fe6dd8-ba31-4d61-89e7-88639da4683d"   # Graph delegated User.Read
SCOPE="mcp-access"     # exposed by the MCP server app
API_SCOPE="Data.Read"  # exposed by the downstream API app

az account show --output none 2>/dev/null || { echo "Run 'az login' first." >&2; exit 1; }
TENANT_ID="$(az account show --query tenantId --output tsv)"

# appId of the app registration named exactly $1, or "" (aborts when several match).
# Exact-name match: `--display-name` is a startswith filter and could hit another app.
find_app() {
  local ids
  ids="$(az ad app list --filter "displayName eq '$1'" --query "[].appId" --output tsv)"
  if [[ "$ids" == *$'\n'* ]]; then
    echo "Several app registrations are named '$1'; choose a unique APP_NAME/API_APP_NAME." >&2
    exit 1
  fi
  echo "$ids"
}

# Expose an API: identifier URI api://<appId>, one delegated scope, v2 access tokens.
# Reuses the scope's id on re-runs (Graph refuses to replace an enabled scope). The PATCH
# replaces the whole scope list: fine for apps dedicated to this example. Sets SCOPE_ID.
expose_api() {  # <appId> <objectId> <scope> <what the scope grants access to>
  az ad app update --id "$1" --identifier-uris "api://$1"
  SCOPE_ID="$(az ad app show --id "$1" \
    --query "api.oauth2PermissionScopes[?value=='$3'].id | [0]" --output tsv)"
  SCOPE_ID="${SCOPE_ID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
  az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$2" \
    --headers "Content-Type=application/json" --body "$(cat <<JSON
{"api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": [{
  "id": "$SCOPE_ID", "value": "$3", "type": "User", "isEnabled": true,
  "adminConsentDisplayName": "Access $4",
  "adminConsentDescription": "Allows the app to call $4 as the signed-in user.",
  "userConsentDisplayName": "Access $4",
  "userConsentDescription": "Allows the app to call $4 on your behalf."}]}}
JSON
)"
}

APP_ID="$(find_app "$APP_NAME")"
API_APP_ID="$(find_app "$API_APP_NAME")"

if [[ "${1:-}" == "--delete" ]]; then
  for pair in "$APP_NAME=$APP_ID" "$API_APP_NAME=$API_APP_ID"; do
    if [[ -n "${pair#*=}" ]]; then
      az ad app delete --id "${pair#*=}"
      echo "Deleted app registration '${pair%%=*}' (${pair#*=})."
    else
      echo "No app registration named '${pair%%=*}'."
    fi
  done
  echo "Remove the AZURE_*, API_CLIENT_ID and DOWNSTREAM_API_URL keys from $ENV_FILE."
  exit 0
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--delete]" >&2; exit 2
fi

# 1. Single-tenant app with a Web redirect URI for FastMCP's OAuth proxy callback.
if [[ -z "$APP_ID" ]]; then
  APP_ID="$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg \
    --web-redirect-uris "$BASE_URL/auth/callback" --query appId --output tsv)"
  echo "Created app registration '$APP_NAME' ($APP_ID)."
else
  echo "Updating existing app registration '$APP_NAME' ($APP_ID)."
  az ad app update --id "$APP_ID" --web-redirect-uris "$BASE_URL/auth/callback"
fi
OBJECT_ID="$(az ad app show --id "$APP_ID" --query id --output tsv)"

# 2. Expose an API: api://<appId>, delegated scope `mcp-access`, v2 tokens.
expose_api "$APP_ID" "$OBJECT_ID" "$SCOPE" "the FastMCP server"

# 3. Delegated Microsoft Graph User.Read (used by the server via OBO).
HAS_USER_READ="$(az ad app show --id "$APP_ID" --query \
  "length(requiredResourceAccess[?resourceAppId=='$GRAPH_APP_ID'].resourceAccess[] | [?id=='$USER_READ_ID'])" \
  --output tsv)"
if [[ "$HAS_USER_READ" == "0" ]]; then
  az ad app permission add --id "$APP_ID" --api "$GRAPH_APP_ID" \
    --api-permissions "$USER_READ_ID=Scope" --output none
fi

# 4. Service principal (enterprise app) so users in this tenant can sign in and consent.
az ad sp show --id "$APP_ID" --output none 2>/dev/null || az ad sp create --id "$APP_ID" --output none

# 5. The downstream API ("the underlying application"): its own app registration exposing
#    `Data.Read`. It only validates tokens, so it needs no redirect URI and no secret.
if [[ -z "$API_APP_ID" ]]; then
  API_APP_ID="$(az ad app create --display-name "$API_APP_NAME" --sign-in-audience AzureADMyOrg \
    --query appId --output tsv)"
  echo "Created app registration '$API_APP_NAME' ($API_APP_ID)."
else
  echo "Updating existing app registration '$API_APP_NAME' ($API_APP_ID)."
fi
API_OBJECT_ID="$(az ad app show --id "$API_APP_ID" --query id --output tsv)"
expose_api "$API_APP_ID" "$API_OBJECT_ID" "$API_SCOPE" "the downstream API"
API_SCOPE_ID="$SCOPE_ID"
az ad sp show --id "$API_APP_ID" --output none 2>/dev/null \
  || az ad sp create --id "$API_APP_ID" --output none

# 6. Let the MCP server get OBO tokens for the API without a second consent (AADSTS65001):
#    the API pre-authorizes the MCP app for Data.Read (separate PATCH: the scope must exist
#    first), and the MCP app declares the permission so `admin-consent` also covers it.
#    The token version is repeated so the API stays on v2 even if `api` were replaced, not merged.
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$API_OBJECT_ID" \
  --headers "Content-Type=application/json" --body "$(cat <<JSON
{"api": {"requestedAccessTokenVersion": 2, "preAuthorizedApplications": [
  {"appId": "$APP_ID", "delegatedPermissionIds": ["$API_SCOPE_ID"]}]}}
JSON
)"
HAS_API_SCOPE="$(az ad app show --id "$APP_ID" --query \
  "length(requiredResourceAccess[?resourceAppId=='$API_APP_ID'].resourceAccess[] | [?id=='$API_SCOPE_ID'])" \
  --output tsv)"
if [[ "$HAS_API_SCOPE" == "0" ]]; then
  az ad app permission add --id "$APP_ID" --api "$API_APP_ID" \
    --api-permissions "$API_SCOPE_ID=Scope" --output none
fi

# 7. Client secret: written straight to .env (mode 600), never printed.
#    Reused on re-runs when .env already holds one for this app, unless NEW_SECRET=1.
SECRET=""
if [[ -f "$ENV_FILE" && "${NEW_SECRET:-0}" != "1" ]] && grep -qx "AZURE_CLIENT_ID=$APP_ID" "$ENV_FILE"; then
  SECRET="$(sed -n 's/^AZURE_CLIENT_SECRET=//p' "$ENV_FILE")"
fi
if [[ -z "$SECRET" ]]; then
  SECRET="$(az ad app credential reset --id "$APP_ID" --append --display-name "$APP_NAME-local" \
    --years 1 --query password --output tsv)"
  echo "Created a new client secret (valid 1 year)."
fi

umask 077
TMP="$(mktemp "$ENV_FILE.XXXXXX")"
if [[ -f "$ENV_FILE" ]]; then
  grep -vE '^(AZURE_TENANT_ID|AZURE_CLIENT_ID|AZURE_CLIENT_SECRET|API_CLIENT_ID|DOWNSTREAM_API_URL|ALLOWED_CLIENT_IDS)=' \
    "$ENV_FILE" > "$TMP" || true
fi
printf 'AZURE_TENANT_ID=%s\nAZURE_CLIENT_ID=%s\nAZURE_CLIENT_SECRET=%s\n' \
  "$TENANT_ID" "$APP_ID" "$SECRET" >> "$TMP"
# ALLOWED_CLIENT_IDS: downstream_api.py accepts tokens only when `azp` is the MCP server.
printf 'API_CLIENT_ID=%s\nDOWNSTREAM_API_URL=%s\nALLOWED_CLIENT_IDS=%s\n' \
  "$API_APP_ID" "$DOWNSTREAM_API_URL" "$APP_ID" >> "$TMP"
mv "$TMP" "$ENV_FILE"
chmod 600 "$ENV_FILE"

cat <<EOF

Done. Wrote AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, API_CLIENT_ID,
DOWNSTREAM_API_URL and ALLOWED_CLIENT_IDS to $ENV_FILE.

Next steps:
  1. Start the downstream API: uv run downstream_api.py
  2. Start the server:         uv run server.py
  3. Connect a client:         claude mcp add --transport http entra-obo $BASE_URL/mcp
  4. Optional, if users may not consent themselves (AADSTS65001), an admin can run:
       az ad app permission admin-consent --id $APP_ID
EOF
