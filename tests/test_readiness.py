import json

import pytest

import multisource_data_agent.main as main


@pytest.mark.asyncio
async def test_readiness_snapshot_requires_core_dependencies_and_llm_config(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        main,
        "_readiness_targets",
        lambda: {
            "database": ("mysql://db:3306/app", 3306),
            "neo4j": ("bolt://graph:7687", 7687),
            "knowledge_service": ("http://kb:8000", 8000),
        },
    )

    async def probe_ok(_url: str, _default_port: int) -> bool:
        return True

    monkeypatch.setattr(main, "_probe_tcp_url", probe_ok)

    ready, checks = await main._readiness_snapshot()

    assert ready is True
    assert checks == {
        "database": "ok",
        "neo4j": "ok",
        "knowledge_service": "ok",
        "llm_config": "ok",
    }


@pytest.mark.asyncio
async def test_readiness_snapshot_reports_dependency_failure_without_secrets(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        main,
        "_readiness_targets",
        lambda: {
            "database": ("mysql://user:secret@db:3306/app", 3306),
            "neo4j": ("bolt://graph:7687", 7687),
            "knowledge_service": ("http://kb:8000", 8000),
        },
    )

    async def selective_probe(url: str, _default_port: int) -> bool:
        return "graph" not in url

    monkeypatch.setattr(main, "_probe_tcp_url", selective_probe)

    ready, checks = await main._readiness_snapshot()

    assert ready is False
    assert checks["neo4j"] == "unreachable"
    assert "secret" not in json.dumps(checks)


@pytest.mark.asyncio
async def test_ready_endpoint_uses_200_and_503_without_affecting_liveness(monkeypatch) -> None:
    async def ready_snapshot():
        return True, {
            "database": "ok",
            "neo4j": "ok",
            "knowledge_service": "ok",
            "llm_config": "ok",
        }

    monkeypatch.setattr(main, "_readiness_snapshot", ready_snapshot)
    response = await main.readiness_check()
    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "ready"

    async def not_ready_snapshot():
        return False, {
            "database": "ok",
            "neo4j": "unreachable",
            "knowledge_service": "ok",
            "llm_config": "ok",
        }

    monkeypatch.setattr(main, "_readiness_snapshot", not_ready_snapshot)
    response = await main.readiness_check()
    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "not_ready"

    assert await main.health_check() == {
        "status": "healthy",
        "version": main.settings.APP_VERSION,
    }
