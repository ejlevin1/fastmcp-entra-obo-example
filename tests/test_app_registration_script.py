"""Run scripts/create_app_registration.sh against a fake `az` that logs its calls."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "create_app_registration.sh"
FAKE_AZ = """#!/usr/bin/env bash
echo "$*" >> "$AZ_LOG"
case "$*" in
  "ad app list --filter displayName eq 'demo-api'"*) printf '%s\\n' $FAKE_API_IDS ;;
  "ad app list"*) printf '%s\\n' $FAKE_APP_IDS ;;
  "ad app create --display-name demo-api "*) echo api-app ;;
  "ad app create"*) echo mcp-app ;;
  "ad app show --id "*" --query id "*) echo "obj-$5" ;;
  *"--query length("*) echo 0 ;;
  "ad app credential reset"*) echo fake-secret ;;
  "account show --query tenantId"*) echo 00000000-0000-0000-0000-000000000000 ;;
esac
exit 0
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run_script(
    tmp_path: Path, *args: str, app_ids: str = "", api_ids: str = ""
) -> tuple[subprocess.CompletedProcess, str]:
    az = tmp_path / "az"
    az.write_text(FAKE_AZ)
    az.chmod(0o755)
    log = tmp_path / "az.log"
    env = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "AZ_LOG": str(log),
        "FAKE_APP_IDS": app_ids,
        "FAKE_API_IDS": api_ids,
        "APP_NAME": "demo",
        "ENV_FILE": str(tmp_path / ".env"),
    }
    result = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)
    return result, log.read_text()


def test_lookup_is_exact_name_match(tmp_path):
    _, calls = run_script(tmp_path, "--delete")
    # `--display-name` is a startswith filter; it could pick e.g. "demo-prod".
    assert "ad app list --filter displayName eq 'demo'" in calls
    assert "ad app list --filter displayName eq 'demo-api'" in calls
    assert "--display-name" not in calls


@pytest.mark.parametrize("ids", [{"app_ids": "aaaa bbbb"}, {"api_ids": "cccc dddd"}])
def test_ambiguous_name_aborts_without_deleting(tmp_path, ids):
    result, calls = run_script(tmp_path, "--delete", **ids)
    assert result.returncode == 1
    assert "Several app registrations" in result.stderr
    assert "ad app delete" not in calls


def test_delete_removes_both_apps(tmp_path):
    result, calls = run_script(tmp_path, "--delete", app_ids="aaaa", api_ids="cccc")
    assert result.returncode == 0
    assert "ad app delete --id aaaa" in calls
    assert "ad app delete --id cccc" in calls


def test_create_wires_the_downstream_api(tmp_path):
    result, calls = run_script(tmp_path)
    assert result.returncode == 0, result.stderr
    # The API app: api://<appId>, v2 tokens, Data.Read, a service principal, no secret.
    assert "ad app create --display-name demo-api --sign-in-audience AzureADMyOrg" in calls
    assert "ad app update --id api-app --identifier-uris api://api-app" in calls
    assert '"requestedAccessTokenVersion": 2' in calls and '"value": "Data.Read"' in calls
    assert "ad sp show --id api-app" in calls
    assert "credential reset --id api-app" not in calls
    # The MCP app is pre-authorized on the API and declares the delegated permission.
    assert "applications/obj-api-app" in calls and '{"appId": "mcp-app"' in calls
    assert "ad app permission add --id mcp-app --api api-app" in calls
    # The pre-authorization PATCH repeats the v2 token version so it can't reset to v1.
    pre_auth = next(c for c in calls.split("rest --method PATCH") if "preAuthorized" in c)
    assert '"requestedAccessTokenVersion": 2' in pre_auth
    env = (tmp_path / ".env").read_text().splitlines()
    assert "API_CLIENT_ID=api-app" in env and "DOWNSTREAM_API_URL=http://localhost:8001" in env
    assert "ALLOWED_CLIENT_IDS=mcp-app" in env  # the API accepts only the MCP server as caller
    assert "fake-secret" not in result.stdout + result.stderr
