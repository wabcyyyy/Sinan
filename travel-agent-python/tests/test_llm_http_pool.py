"""LLM httpx 共享客户端：连接池存在性与关闭行为。"""

from app.common import llm_client


def test_shared_client_reused(monkeypatch):
    llm_client.close_http_client()
    a = llm_client._get_http_client()
    b = llm_client._get_http_client()
    assert a is b
    assert not a.is_closed
    llm_client.close_http_client()
    assert llm_client._http_client is None


def test_close_is_idempotent():
    llm_client.close_http_client()
    llm_client.close_http_client()
    c = llm_client._get_http_client()
    assert c is not None
    llm_client.close_http_client()
