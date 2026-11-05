"""Provider JSON errors must preserve the normal HTTP execution contract."""

import httpx
import pytest

from multisource_data_agent.interfaces.http.v1 import chat as chat_module
from multisource_data_agent.main import application


@pytest.mark.asyncio
@pytest.mark.parametrize("error_text", [
    "Error code: 400 - {'error': {'code': 'InvalidParameter', 'message': 'fixture failure'}}",
    'Provider response: {"error": {"code": "ServiceUnavailable"}}',
])
async def test_provider_json_error_returns_execution_error_instead_of_http_500(monkeypatch, error_text):
    calls = []

    class FailingGraph:
        async def ainvoke(self, input_state, config=None):
            calls.append(input_state)
            raise RuntimeError(error_text)

    monkeypatch.setattr(chat_module, "graph", FailingGraph())
    monkeypatch.setattr(chat_module, "get_or_create_session", lambda *_args: "error-contract-test")

    async def save_message(*_args, **_kwargs):
        return "error-message"

    monkeypatch.setattr(chat_module, "save_message", save_message)
    previous = application.dependency_overrides.copy()
    application.dependency_overrides[chat_module.get_db] = lambda: object()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            response = await client.post("/api/v1/chat/", json={"message": "查询当前订单数"})
    finally:
        application.dependency_overrides.clear()
        application.dependency_overrides.update(previous)

    assert len(calls) == 1
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "error"
    assert body["execution"]["contract_version"] == "1"
    assert body["execution"]["status"] == "error"
    assert body["execution"]["evidence"] == []
    assert body["metadata"]["error"] == error_text
