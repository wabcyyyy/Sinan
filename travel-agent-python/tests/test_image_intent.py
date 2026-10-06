"""图像意图理解（POST /api/image-intent）单测：配额闸 / 大小与格式防线 / BYOK 路由 / 全 mock。

夹具抄 tests/test_cover_api.py 与 tests/test_llm_gateway_api.py（TestClient + sqlite +
Bearer 头）；视觉调用 mock 掉 agent 模块的 `get_role_client`（2026-10-04 起视觉角色由
model_registry 解析）——全部用例零外网。视觉角色显式指向百炼兼容层：默认复用 main 时
主模型在 DeepSeek 官方，qwen-vl-plus 在那里并不存在，能力闸会先拦下来（那正是它的职责）。
429 专项按 tests/test_quota_and_headers.py:39-47 的手法自行 monkeypatch 回小值
（conftest 的 _neutralize_ip_and_user_limits 默认把窗口放到 100_000）。
"""

from __future__ import annotations

import base64
import io
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.agent  # noqa: F401  # 先行破存量导入环（llm_client→门面→web_search→llm_client，同 tests/test_llm_backoff.py 口径）
from app.agent.editing import image_intent as image_intent_agent
from app.api import deps
from app.api.business.image_intent import router as image_intent_router
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.common.jwt_compat import encode_token
from app.db import session as db_session
from app.db.models import Base, UserLlmGateway
from app.services import image_intent as image_intent_service

SIGNING_MATERIAL = "example-only-hs256-signing-material-32b"  # 测试占位串，非真实凭据

_REPLY = '{"text": "图里是西湖断桥的雪景。", "suggestedMessage": "想去杭州看断桥残雪，帮我安排 3 天行程"}'

#: 视觉角色落点：与替身的 base_url 同源（百炼兼容层提供 qwen-vl-*）
_VISION_ROLE = "dashscope:qwen-vl-plus"


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    engine = create_engine(f"sqlite:///{tmp_path / 'image_intent.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))

    monkeypatch.setattr(deps.settings, "jwt_secret", SIGNING_MATERIAL)
    monkeypatch.setattr(deps.token_revocation, "is_revoked", lambda _t: False)
    monkeypatch.setattr(
        deps.user_repository,
        "find_by_username",
        lambda _u: {"id": 42, "username": "alice", "role": "user", "status": 1},
    )

    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(image_intent_router)
    yield TestClient(app)
    db_session.init_engine(None, None)


class _CapturingClient:
    """替身 LLMClient：记录 chat 入参并返回固定回复（覆盖 json/图片两个 part 的断言面）。

    base_url 是百炼兼容层口径：视觉模型只在"该通道真提供这个模型名"时才允许发出去
    （require_model_served）；替身不带 base_url 会被能力闸拦在 AttributeError 上。
    """

    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def __init__(self, reply: str = _REPLY) -> None:
        self.reply = reply
        self.calls: list[tuple[list, dict]] = []

    def chat(self, messages: list, **kwargs) -> str:
        self.calls.append((messages, kwargs))
        return self.reply


@pytest.fixture(autouse=True)
def _vision_role_on_dashscope(monkeypatch):
    """视觉角色固定指向百炼兼容层（与替身 base_url 同源）。

    默认"留空复用 main"时主模型在 DeepSeek 官方，而 qwen-vl-plus 在那里并不存在，
    能力闸会先拦下——那是它该做的事，但不该让"图像链路本身"的用例全部因此变红。
    角色与模型不匹配的拦截另有 tests/test_provider_capability.py 专门覆盖。
    """
    monkeypatch.setattr(settings, "llm_role_vision", _VISION_ROLE)


@pytest.fixture
def fake_client(monkeypatch) -> _CapturingClient:
    """替身注入点：视觉走 `get_role_client("vision")`（2026-10-04 起由注册表解析）。"""
    fake = _CapturingClient()
    monkeypatch.setattr(image_intent_agent, "get_role_client", lambda _role: fake)
    return fake


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_token('alice', SIGNING_MATERIAL, 3600)}"}


def _png_bytes(size: tuple[int, int] = (3000, 2000)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 90, 140)).save(buffer, "PNG")
    return buffer.getvalue()


def _gif_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buffer, "GIF")
    return buffer.getvalue()


def _post(client: TestClient, data: bytes, content_type: str = "image/png", context: str | None = None):
    files = {"file": ("shot.png", data, content_type)}
    if context is not None:
        return client.post("/api/image-intent", headers=_headers(), files=files, data={"context": context})
    return client.post("/api/image-intent", headers=_headers(), files=files)


# ---------- 200 主链路：payload / 模型选择 / 压缩 ----------


def test_interpret_ok_sends_data_uri_and_vision_model(
    client: TestClient, fake_client: _CapturingClient, monkeypatch
) -> None:
    response = _post(client, _png_bytes(), context="想去杭州玩三天")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["text"] == "图里是西湖断桥的雪景。"
    assert data["suggestedMessage"].startswith("想去杭州")

    messages, kwargs = fake_client.calls[0]
    assert kwargs["model"] == "qwen-vl-plus", "无 BYOK 路由时用默认视觉模型"
    assert kwargs["json_mode"] is True and kwargs["temperature"] == 0.2 and kwargs["max_tokens"] == 512
    content = messages[0]["content"]
    assert content[0]["type"] == "image_url"
    url = content[0]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert "想去杭州玩三天" in content[1]["text"], "对话背景要随提示词进 text part"

    # 重编码链路：3000x2000 PNG → JPEG 且长边 ≤2048
    decoded = base64.b64decode(url.split(",", 1)[1])
    with Image.open(io.BytesIO(decoded)) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= image_intent_service.IMAGE_INTENT_MAX_DIM


def test_interpret_byok_route_uses_user_model(client: TestClient, fake_client: _CapturingClient) -> None:
    from app.common import gateway_crypto

    with db_session.session_scope() as session:
        session.add(
            UserLlmGateway(
                user_id=42,
                name="自带网关",
                base_url="https://gateway.example/v1",
                api_key_cipher=gateway_crypto.encrypt_secret("sk-test-cccc3333"),
                api_key_hint="3333",
                model="my-vision-model",
                enabled=True,
            )
        )
    response = _post(client, _png_bytes())
    assert response.status_code == 200, response.text
    _messages, kwargs = fake_client.calls[0]
    assert kwargs["model"] == "my-vision-model", "BYOK 路由生效时模型跟随用户网关配置"


def test_interpret_accepts_webp(client: TestClient, fake_client: _CapturingClient) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (9, 99, 199)).save(buffer, "WEBP")
    response = _post(client, buffer.getvalue(), content_type="image/webp")
    assert response.status_code == 200, response.text
    assert len(fake_client.calls) == 1


# ---------- 入口防线：鉴权 / 大小 / 格式 / 配额 ----------


def test_interpret_requires_auth(client: TestClient) -> None:
    response = client.post("/api/image-intent", files={"file": ("x.png", _png_bytes(), "image/png")})
    assert response.status_code == 401


def test_interpret_rejects_oversize_413(client: TestClient, fake_client: _CapturingClient) -> None:
    response = _post(client, b"0" * (image_intent_service.IMAGE_INTENT_MAX_BYTES + 1))
    assert response.status_code == 413
    assert not fake_client.calls, "超限不得打到视觉模型上"


def test_interpret_rejects_broken_image_400(client: TestClient, fake_client: _CapturingClient) -> None:
    response = _post(client, b"GIF89a-not-an-image", content_type="image/gif")
    assert response.status_code == 400
    assert not fake_client.calls


def test_interpret_rejects_gif_format_400(client: TestClient, fake_client: _CapturingClient) -> None:
    response = _post(client, _gif_bytes(), content_type="image/gif")
    assert response.status_code == 400
    assert "jpeg/png/webp" in response.json()["message"]
    assert not fake_client.calls


def test_interpret_rejects_pixel_bomb_400(client: TestClient, fake_client: _CapturingClient, monkeypatch) -> None:
    """像素上限在 convert/thumbnail 放大内存之前拦截（阈值缩到 100 便于离线验证分支）。"""
    monkeypatch.setattr(image_intent_service, "IMAGE_INTENT_MAX_PIXELS", 100)
    response = _post(client, _png_bytes())  # 3000x2000 = 6M 像素
    assert response.status_code == 400
    assert not fake_client.calls, "像素超限不得进重编码与视觉模型"


def test_interpret_quota_429_before_any_work(client: TestClient, fake_client: _CapturingClient, monkeypatch) -> None:
    """配额闸在解析/读体之前：小窗下第二次请求 429，且视觉模型只被调用一次。"""
    from app.common.config import settings

    monkeypatch.setattr(settings, "user_llm_runs_per_minute", 1)
    monkeypatch.setattr(settings, "user_daily_llm_runs", 10_000)
    # 独立 uid：进程内限速表整个 pytest 会话共享，user 42 已被前面的用例记过命中
    monkeypatch.setattr(
        deps.user_repository,
        "find_by_username",
        lambda _u: {"id": 432_143, "username": "alice", "role": "user", "status": 1},
    )

    assert _post(client, _png_bytes()).status_code == 200
    assert _post(client, _png_bytes()).status_code == 429
    assert len(fake_client.calls) == 1

    monkeypatch.setattr(settings, "user_llm_runs_per_minute", 0)
    assert _post(client, b"GIF89a-not-an-image", content_type="image/gif").status_code == 429, "闸在格式校验之前"


# ---------- 失败面：解析失败 / 上游异常 ----------


def test_interpret_parse_failure_is_502(client: TestClient, monkeypatch) -> None:
    fake = _CapturingClient(reply="这不是 JSON")
    monkeypatch.setattr(image_intent_agent, "get_role_client", lambda _role: fake)
    response = _post(client, _png_bytes())
    assert response.status_code == 502
    assert "没能识别这张图片" in response.json()["message"]


def test_interpret_upstream_failure_is_generic_502(client: TestClient, monkeypatch) -> None:
    class _Boom:
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

        def chat(self, *_args, **_kwargs) -> str:
            raise RuntimeError("upstream 503 for url https://gateway.example/v1/chat/completions")

    monkeypatch.setattr(image_intent_agent, "get_role_client", lambda _role: _Boom())
    response = _post(client, _png_bytes())
    assert response.status_code == 502
    assert response.json()["message"] == "图像识别服务暂不可用", "上游异常不得透传"


# ---------- agent 层纯逻辑 ----------


def test_run_image_intent_parses_fenced_json(monkeypatch) -> None:
    fake = _CapturingClient(reply='```json\n{"text": "海边日落", "suggestedMessage": "想去海边看日落"}\n```')
    monkeypatch.setattr(image_intent_agent, "get_role_client", lambda _role: fake)
    result = image_intent_agent.run_image_intent("data:image/jpeg;base64,aGVsbG8=")
    assert result == {"text": "海边日落", "suggestedMessage": "想去海边看日落"}


def test_run_image_intent_falls_back_suggested_to_text(monkeypatch) -> None:
    fake = _CapturingClient(reply='{"text": "一碗热气腾腾的牛肉面"}')
    monkeypatch.setattr(image_intent_agent, "get_role_client", lambda _role: fake)
    result = image_intent_agent.run_image_intent("data:image/jpeg;base64,aGVsbG8=")
    assert result["suggestedMessage"] == "一碗热气腾腾的牛肉面"


def test_run_image_intent_rejects_garbage(monkeypatch) -> None:
    fake = _CapturingClient(reply="完全不是 JSON")
    monkeypatch.setattr(image_intent_agent, "get_role_client", lambda _role: fake)
    with pytest.raises(ValueError):
        image_intent_agent.run_image_intent("data:image/jpeg;base64,aGVsbG8=")


def test_run_image_intent_rejects_empty_text(monkeypatch) -> None:
    fake = _CapturingClient(reply='{"text": "", "suggestedMessage": "x"}')
    monkeypatch.setattr(image_intent_agent, "get_role_client", lambda _role: fake)
    with pytest.raises(ValueError):
        image_intent_agent.run_image_intent("data:image/jpeg;base64,aGVsbG8=")


# ---------- 装配回归 ----------


def test_router_mounted_on_real_app() -> None:
    """回归防线：business_routers 漏注册时端点会 404 而单域测试仍绿。"""
    import main

    paths = {getattr(route, "path", None) for route in main.app.routes}
    assert "/api/image-intent" in paths
