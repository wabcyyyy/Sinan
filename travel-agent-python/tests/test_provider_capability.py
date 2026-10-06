"""模型角色注册表与供应商能力闸（2026-10-04）。

背景：主模型换到 DeepSeek 官方后暴露出三类问题——

1. **配置散**：`settings.llm_fast_model` 被 7 个模块 15 处各自读取，`settings.llm_api_key`
   被当"有没有配 LLM"的哨兵散落 10 处，每个新用途（语音）都要再加一组平铺 env 键；
2. **失效静默**：联网搜索的 `enable_search` 是百炼插件，别的通道丢参数后模型拿记忆冒充
   检索结果（来源还标 web.search）；qwen-vl-plus / qwen-max 打到不提供它们的网关；
3. **参数语义含糊**：`.env` 里 `LLM_REASONING_EFFORT=`（留空）解析成 `""` 而非 None，
   于是思考关不掉、空串还会被当有效强度发出去。

本文件钉住收敛后的口径：**唯一解析点是 app/common/model_registry.py**；能力由 provider
声明（不再散在调用点）；配错在启动期或发请求前响亮报错，不伪装成上游故障。
"""

from __future__ import annotations

import pytest

import app.agent  # noqa: F401 先完成 agent 门面导入
from app.agent.data import pricing, web_search
from app.agent.editing import image_intent as image_intent_agent
from app.common import model_registry
from app.common.config import settings
from app.common.llm_client import (
    _apply_provider_options,
    channel_serves_model,
    channel_supports_search,
    get_judge_client,
    get_role_client,
    require_model_served,
)
from app.common.llm_route import LLMRoute, use_route

DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEEPSEEK = "https://api.deepseek.com"
BYOK = "https://gateway.example/v1"


@pytest.fixture(autouse=True)
def _reset_warn_latch():
    """每次用例重置"已告警通道"闩：否则前一条用例消耗掉的那次 warning 不再出现。"""
    web_search._unavailable_warned.clear()
    yield
    web_search._unavailable_warned.clear()


class _ForbiddenClient:
    """哨兵：任何 LLM 调用都算用例失败（"判否就一次外呼都不发"的判据）。"""

    base_url = DEEPSEEK

    def __getattr__(self, name: str):
        raise AssertionError(f"通道不支持该能力时不得发起 LLM 调用（触发了 {name}）")


# ---------- 1. 通道能力判据（底层，供注册表使用） ----------


def test_channel_supports_search_only_for_dashscope():
    assert channel_supports_search(DASHSCOPE) is True
    assert channel_supports_search(DEEPSEEK) is False
    assert channel_supports_search(BYOK) is False


def test_provider_options_refuse_search_on_incapable_channel():
    with pytest.raises(ValueError, match="不支持联网搜索"):
        _apply_provider_options({"messages": []}, DEEPSEEK, enable_search=True)
    with pytest.raises(ValueError, match="不支持联网搜索"):
        _apply_provider_options({"messages": []}, BYOK, enable_search=True)
    payload: dict = {"messages": []}
    _apply_provider_options(payload, DASHSCOPE, enable_search=True)
    assert payload["enable_search"] is True


def test_channel_serves_model_by_provider():
    assert channel_serves_model(DEEPSEEK, "deepseek-flash") is True
    assert channel_serves_model(DEEPSEEK, "qwen-vl-plus") is False
    assert channel_serves_model(DASHSCOPE, "qwen-max") is True
    assert channel_serves_model(DASHSCOPE, "deepseek-chat") is False
    assert channel_serves_model(BYOK, "my-vision-model") is True
    assert channel_serves_model(BYOK, "") is False  # 没配模型 = 配置错，不当"自建网关"放行


def test_require_model_served_raises_with_actionable_message():
    require_model_served(DEEPSEEK, "deepseek-flash")  # 匹配则不抛
    with pytest.raises(ValueError, match="不提供模型 qwen-vl-plus"):
        require_model_served(DEEPSEEK, "qwen-vl-plus")


# ---------- 2. 角色解析：provider:model / 裸名 / 留空回落 ----------


def test_roles_fall_back_to_main_by_default(monkeypatch):
    """不配角色键 = 全部走 main（与迁移前行为一致，零回归）。

    显式置空六个角色键：本机 .env 可能真的配了角色（如 LLM_ROLE_STT），
    回退路径的判定不许依赖环境巧合。
    """
    for role in ("main", "fast", "vision", "judge", "search", "stt"):
        monkeypatch.setattr(settings, f"llm_role_{role}", "")
    for role in ("fast", "vision", "judge", "search", "stt"):
        bound = model_registry.binding(role)
        assert bound.model == settings.llm_model
        assert bound.base_url == settings.llm_base_url


def test_bare_model_name_means_default_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_role_fast", "some-cheap-model")
    bound = model_registry.binding("fast")
    assert (bound.provider.name, bound.model) == ("default", "some-cheap-model")


def test_provider_prefixed_role_binds_that_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", DASHSCOPE)
    monkeypatch.setattr(settings, "llm_provider_dashscope_api_key", "dash-key")
    monkeypatch.setattr(settings, "llm_role_search", "dashscope:qwen-plus")

    bound = model_registry.binding("search")
    assert (bound.provider.name, bound.model, bound.base_url) == ("dashscope", "qwen-plus", DASHSCOPE)
    assert "search" in bound.capabilities()
    assert model_registry.configured("search") is True


def test_unknown_provider_and_missing_model_are_loud(monkeypatch):
    monkeypatch.setattr(settings, "llm_role_fast", "nope:some-model")
    with pytest.raises(ValueError, match="未知 provider"):
        model_registry.binding("fast")

    monkeypatch.setattr(settings, "llm_role_fast", "dashscope:")
    with pytest.raises(ValueError, match="缺模型名"):
        model_registry.binding("fast")

    with pytest.raises(ValueError, match="未知角色"):
        model_registry.binding("nope")


def test_validate_rejects_role_pointing_at_unconfigured_provider(monkeypatch):
    """启动期就该拦下：角色指向没配 base_url 的 provider。"""
    monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", "")
    monkeypatch.setattr(settings, "llm_role_search", "dashscope:qwen-plus")

    with pytest.raises(ValueError, match="没配 base_url"):
        model_registry.validate()


def test_validate_passes_on_default_config(monkeypatch):
    """默认全走 main 时 validate 必须能过。

    显式置空六个角色键（同 test_roles_fall_back_to_main_by_default 口径）：本机 .env
    可能真配了指向未配 base_url provider 的角色——那是配置错，该在真实启动时由 validate
    报出；本用例只钉"零角色配置能过"，不许依赖环境巧合。
    """
    for role in ("main", "fast", "vision", "judge", "search", "stt"):
        monkeypatch.setattr(settings, f"llm_role_{role}", "")
    model_registry.validate()


def test_byok_route_overrides_every_role(monkeypatch):
    """BYOK 是用户自己的网关：它盖过部署默认的角色绑定。"""
    monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", DASHSCOPE)
    monkeypatch.setattr(settings, "llm_role_fast", "dashscope:qwen-plus")

    with use_route(LLMRoute(base_url=BYOK, api_key="user-key", model="user-model")):
        bound = model_registry.binding("fast")
        assert (bound.base_url, bound.model) == (BYOK, "user-model")
        assert model_registry.configured("fast") is True
        assert get_role_client("fast").base_url == BYOK


# ---------- 3. 联网搜索 / 实时价：判否即降级，不发外呼 ----------


def test_search_channel_ready_is_false_on_deepseek_and_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(settings, "llm_base_url", DEEPSEEK)

    assert web_search.search_channel_ready() is False
    assert "联网搜索不可用" in caplog.text
    caplog.clear()
    assert web_search.search_channel_ready() is False
    assert caplog.text == "", "同一通道只许嚷嚷一次，不许每次调用都刷屏"


def test_search_channel_ready_true_when_role_points_at_dashscope(monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", DEEPSEEK)  # 主模型在 DeepSeek
    monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", DASHSCOPE)
    monkeypatch.setattr(settings, "llm_role_search", "dashscope:qwen-plus")

    assert web_search.search_channel_ready() is True


def test_web_search_entrypoints_degrade_without_calling_llm(monkeypatch):
    """判否 → 空结果，且一次 LLM 调用都不发（旧行为是发出去、拿记忆冒充检索）。"""
    monkeypatch.setattr(settings, "llm_base_url", DEEPSEEK)
    monkeypatch.setattr(settings, "web_search_enabled", True)
    monkeypatch.setattr(settings, "llm_api_key", "configured")
    monkeypatch.setattr(web_search, "get_role_client", lambda _role: _ForbiddenClient())

    assert web_search.web_search_text("成都有哪些酒店？") == ""
    assert web_search.web_search_json("成都有哪些酒店？", schema_hint="{}") is None
    assert web_search.search_places_via_web("成都", "hotel") == []


def test_live_price_degrades_without_calling_llm(monkeypatch):
    monkeypatch.setattr(pricing, "search_channel_ready", lambda: False)
    monkeypatch.setattr(pricing, "get_role_client", lambda _role: _ForbiddenClient())

    assert pricing.query_live_price("成都", "某某酒店", None) is None
    assert pricing.query_live_food_price("成都", "某某餐厅") is None


# ---------- 4. 视觉角色：通道不供该模型就叫停 ----------


def test_image_intent_rejects_model_channel_mismatch(monkeypatch):
    """视角角色没配 = 复用 main（DeepSeek），而 DeepSeek 不提供 qwen-vl-plus。"""
    monkeypatch.setattr(settings, "llm_base_url", DEEPSEEK)
    monkeypatch.setattr(settings, "llm_role_vision", "qwen-vl-plus")  # 裸名 → default provider
    monkeypatch.setattr(image_intent_agent, "get_role_client", lambda _role: _ForbiddenClient())

    with pytest.raises(ValueError, match="不提供模型 qwen-vl-plus"):
        image_intent_agent.run_image_intent("data:image/jpeg;base64,aGVsbG8=")


# ---------- 5. judge 角色 ----------


def test_judge_client_refuses_unserved_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", DEEPSEEK)
    monkeypatch.setattr(settings, "llm_role_judge", "qwen-max")  # 裸名 → 打到 DeepSeek

    with pytest.raises(ValueError, match="不提供模型 qwen-max"):
        get_judge_client()


def test_judge_client_ok_on_its_own_gateway(monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", DEEPSEEK)
    monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", DASHSCOPE)
    monkeypatch.setattr(settings, "llm_provider_dashscope_api_key", "dash-key")
    monkeypatch.setattr(settings, "llm_role_judge", "dashscope:qwen-max")

    assert get_judge_client().base_url == DASHSCOPE


def test_judge_client_ok_when_role_falls_back_to_a_serving_main(monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", DEEPSEEK)
    monkeypatch.setattr(settings, "llm_role_judge", "deepseek-chat")

    assert get_judge_client().base_url == DEEPSEEK


# ---------- 6. json_schema 强约束档能力位（夜审 R2-F1，2026-10-05） ----------


def test_json_schema_capability_degrades_deepseek_official(monkeypatch):
    """DeepSeek 官方 2026-10-05 实测对 response_format json_schema 返回 400（同 key
    同模型 json_object 200，夜审 R2-F1 探针）：该网关不得带 json_schema 位，生成出口
    与 judge 据此降级 json_object。判据是网关 URL（api.deepseek.com）而非名册名——
    BYOK 指向同一网关同样降级。其余网关维持支持口径（与降级前行为一致，不为未探测
    的网关猜能力）；DashScope 的 json_schema 支持是 PR-5 spike（2026-09-24）实证的。"""
    monkeypatch.setattr(settings, "llm_base_url", DEEPSEEK)
    caps = model_registry.provider("default").capabilities
    assert "chat" in caps and "json_schema" not in caps
    with use_route(LLMRoute(base_url=DEEPSEEK, api_key="user-key", model="deepseek-chat")):
        assert "json_schema" not in model_registry.binding("fast").capabilities()

    monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", DASHSCOPE)
    monkeypatch.setattr(settings, "llm_provider_dashscope_api_key", "dash-key")
    assert {"chat", "search", "json_schema"} <= model_registry.provider("dashscope").capabilities


def test_generation_exit_response_format_follows_capability(monkeypatch):
    """消费方分支实证（能力位不许是死数据，R1-F1 口径）：DeepSeek → json_object；
    守约网关 → strict json_schema。"""
    from app.common.model_registry import json_response_format

    monkeypatch.setattr(settings, "llm_base_url", DEEPSEEK)
    for role in ("main", "fast", "vision", "judge", "search", "stt"):
        monkeypatch.setattr(settings, f"llm_role_{role}", "")
    assert json_response_format("fast", "day_output", {"type": "object"}) == {"type": "json_object"}

    monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", DASHSCOPE)
    monkeypatch.setattr(settings, "llm_provider_dashscope_api_key", "dash-key")
    monkeypatch.setattr(settings, "llm_role_fast", "dashscope:qwen-plus")
    fmt = json_response_format("fast", "day_output", {"type": "object"})
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
