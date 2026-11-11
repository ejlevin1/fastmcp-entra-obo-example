import os

# Dummy, non-secret values so `import server` and `import downstream_api` work without a tenant.
os.environ.update(
    AZURE_TENANT_ID="00000000-0000-0000-0000-000000000000",
    AZURE_CLIENT_ID="11111111-1111-1111-1111-111111111111",
    AZURE_CLIENT_SECRET="dummy-secret",
    BASE_URL="http://localhost:8000",
    API_CLIENT_ID="22222222-2222-2222-2222-222222222222",
    DOWNSTREAM_API_URL="http://localhost:8001",
    ALLOWED_CLIENT_IDS="",  # tests set the allow-list explicitly; a local .env must not leak in
)
