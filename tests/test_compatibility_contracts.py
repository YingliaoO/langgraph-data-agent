"""
Compatibility contract tests.

These tests intentionally avoid calling external services (LLMs, databases).
They only verify that we keep stable public-facing interfaces (routes/env vars)
even as internals evolve.
"""


import sys
from pathlib import Path
from typing import Set, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _collect_route_methods(app) -> Set[Tuple[str, str]]:
    methods: Set[Tuple[str, str]] = set()
    http_methods = {
        "get", "post", "put", "patch",
        "delete", "options", "head", "trace",
    }

    for path, path_item in app.openapi().get("paths", {}).items():
        for method in http_methods.intersection(path_item.keys()):
            methods.add((path, method.upper()))

    return methods


def test_chat_api_exposes_legacy_alias_routes() -> None:
    from fastapi.testclient import TestClient
    from multisource_data_agent.main import application

    client = TestClient(application)

    cases = [
        ("POST", "/api/v1/chat/chat", {"json": {}}),
        ("GET", "/api/v1/chat/chat/stream", {}),
        ("POST", "/api/v1/chat/chat/stream", {"json": {}}),
    ]

    for method, path, kwargs in cases:
        response = client.request(method, path, **kwargs)

        assert response.status_code == 422, (
            f"{method} {path}: expected 422 for missing required fields, "
            f"got {response.status_code}: {response.text}"
        )


def test_settings_accept_openai_env_var_aliases(monkeypatch) -> None:
    """
    The codebase uses Settings.LLM_* but older deployments use OPENAI_*.
    Support both to reduce migration friction.
    """

    # Ensure the canonical LLM_* vars are not set (other tests may load `.env`).
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENAI_API_BASE", "http://example.local/v1")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")

    # Import late so env is set before instantiation.
    from multisource_data_agent.config.settings import Settings

    settings = Settings(_env_file=None)

    assert settings.LLM_API_KEY == "test-openai-key"
    assert settings.LLM_BASE_URL == "http://example.local/v1"
    assert settings.LLM_MODEL == "test-model"
