"""
Pytest configuration.

Make the project importable in all environments without requiring an editable install.
Some CI/runner setups use `--import-mode=importlib` or otherwise do not add the repo root
to `sys.path` consistently, so we do it explicitly here.
"""


import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


import pytest


@pytest.fixture
def api_bearer_headers(monkeypatch):
    """Authenticate existing positive HTTP contract tests without bypassing JWT verification."""
    from datetime import datetime, timedelta, timezone
    from jose import jwt
    from multisource_data_agent.config import settings
    secret = "contract-test-key-not-a-deployment-key-1234567890"
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", secret)
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": "contract-user", "iat": now, "exp": now + timedelta(minutes=5),
                        "iss": settings.AUTH_JWT_ISSUER, "aud": settings.AUTH_JWT_AUDIENCE}, secret, algorithm="HS256")
    return {"Authorization": "Bearer " + token}
