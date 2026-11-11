#!/usr/bin/env bash
# Smoke test: bring the compose stack up with the dummy .env.example values and check auth is
# enforced. Works the same locally and in CI. Needs docker; never touches Entra.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { cp .env.example .env; made_env=1; }
cleanup() {
  docker compose down -v --rmi local
  [[ -z "${made_env:-}" ]] || rm -f .env
}
trap cleanup EXIT

docker compose up -d --build --wait --wait-timeout 120

check() { # check <description> <expected status> <curl args...>
  local desc=$1 want=$2 got
  shift 2
  got=$(curl -s -o /dev/null -w '%{http_code}' "$@")
  [[ "$got" == "$want" ]] || { echo "FAIL: $desc (got $got, want $want)"; exit 1; }
  echo "ok: $desc -> $got"
}

check "PRM document" 200 http://localhost:8000/.well-known/oauth-protected-resource/mcp
prm=$(curl -s http://localhost:8000/.well-known/oauth-protected-resource/mcp)
python3 - "$prm" <<'PY'
import json, sys
scopes = json.loads(sys.argv[1]).get("scopes_supported")
assert scopes == ["mcp-access"], scopes
print("ok: PRM advertises only", scopes)
PY
check "POST /mcp without token" 401 -X POST http://localhost:8000/mcp \
  -H 'content-type: application/json' -d '{}'
check "API without token" 401 http://localhost:8001/api/profile
echo "All compose smoke checks passed."
