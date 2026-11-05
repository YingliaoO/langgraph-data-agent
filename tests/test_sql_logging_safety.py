"""Configured connection strings must never enter SQL diagnostic logs."""

import pytest
from loguru import logger

from multisource_data_agent.application.agents.text2sql.components.sql_execution import node


@pytest.mark.parametrize("url", [
    "mysql+pymysql://demo:fake-secret-123@localhost/retail",
    "postgresql://demo:fake-secret-123@localhost/retail?application_name=private",
])
def test_connection_fallback_does_not_log_credentials(monkeypatch, url):
    monkeypatch.setattr(node.settings, "BUSINESS_DATABASE_URL", url)
    monkeypatch.setattr(node.settings, "BUSINESS_ALLOWED_SCHEMAS", "retail")
    messages = []
    sink = logger.add(messages.append, level="DEBUG", format="{message}")
    try:
        if url.startswith("mysql"):
            assert node._get_connection_string(None) == url
        else:
            with pytest.raises(ValueError):
                node._get_connection_string(None)
    finally:
        logger.remove(sink)
    output = "\n".join(str(message) for message in messages)
    assert output  # Exercise the debug log rather than silencing it.
    assert "fake-secret-123" not in output
    assert url not in output
    assert "private" not in output
