"""poi_intros 生产路径上的 Function Calling 集成测试。"""

import json

from app.agent.generation.content import butler
from app.agent.tools.function_calling import FunctionCallingError

LONG_INTRO = "西湖" * 100  # ≥180 字：达到 200~300 字契约下限，不触发扩写轮


class FakeLlm:
    """模拟带 tools 的 LLM：先 tool_calls，再最终 JSON content。"""

    def __init__(self, mode: str = "fc"):
        self.mode = mode
        self.chat_calls = []
        self.complete_calls = []

    def chat_response(self, messages, **kwargs):
        self.chat_calls.append({"messages": messages, "kwargs": kwargs})
        if self.mode == "fc_tool":
            if len(self.chat_calls) == 1:
                return {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "search_pois",
                                    "arguments": json.dumps({"city": "杭州", "preferences": ["西湖"]}),
                                },
                            }
                        ],
                    }
                }
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"intros": {"西湖": LONG_INTRO}}, ensure_ascii=False),
                }
            }
        if self.mode == "fc_fail":
            raise FunctionCallingError("达到最大轮数")
        # 直接出答案
        return {
            "message": {
                "role": "assistant",
                "content": json.dumps({"intros": {"雷峰塔": "登塔俯瞰西湖。"}}, ensure_ascii=False),
            }
        }

    def complete(self, user_prompt, **kwargs):
        self.complete_calls.append(user_prompt)
        if self.mode == "fc_tool":
            return json.dumps({"intros": {"西湖": "西湖（扩写后）" + "湖光山色" * 60}}, ensure_ascii=False)
        return json.dumps({"intros": {"雷峰塔": "单轮兜底介绍。"}}, ensure_ascii=False)


def test_poi_intros_uses_function_calling_then_returns_intros(monkeypatch):
    fake = FakeLlm("fc_tool")
    monkeypatch.setattr(butler, "get_llm_client", lambda: fake)
    invoked = []

    def fake_invoke(name, arguments, **kwargs):
        invoked.append((name, arguments))
        return {"city": arguments.get("city"), "items": [{"name": "西湖"}]}

    monkeypatch.setattr("app.agent.tools.registry.registry.invoke", fake_invoke)
    intros = butler.run_poi_intros("杭州", ["西湖"])
    assert intros["西湖"].startswith("西湖")  # FC 成功路径返回长介绍（达标，不触发扩写轮）
    assert invoked and invoked[0][0] == "search_pois"
    # 走了 FC：至少一次 chat_response 且带 tools schema
    assert fake.chat_calls
    assert fake.chat_calls[0]["kwargs"].get("tools")
    assert not fake.complete_calls


def test_poi_intros_falls_back_when_fc_fails(monkeypatch):
    fake = FakeLlm("fc_fail")
    monkeypatch.setattr(butler, "get_llm_client", lambda: fake)
    intros = butler.run_poi_intros("杭州", ["雷峰塔"])
    assert intros["雷峰塔"] == "单轮兜底介绍。"
    assert fake.complete_calls


def test_poi_intros_rewrites_short_intro(monkeypatch):
    """介绍过短（<180 字）时触发扩写轮：单轮兜底先产出短介绍，扩写轮取更长版本。"""
    fake = FakeLlm("fc_fail")
    monkeypatch.setattr(butler, "get_llm_client", lambda: fake)
    calls = {"complete": 0}

    def complete(user_prompt, **kwargs):
        fake.complete_calls.append(user_prompt)
        calls["complete"] += 1
        if calls["complete"] == 1:
            # 兜底轮：介绍过短
            return json.dumps({"intros": {"西湖": "杭州名片。"}}, ensure_ascii=False)
        # 扩写轮：达标长介绍
        return json.dumps({"intros": {"西湖": "西湖扩写版。" + "三面云山一面城，" * 40}}, ensure_ascii=False)

    fake.complete = complete
    intros = butler.run_poi_intros("杭州", ["西湖"])
    assert len(fake.complete_calls) == 2
    assert intros["西湖"].startswith("西湖扩写版。")
    assert len(intros["西湖"]) >= 180


def test_poi_intros_keeps_intro_when_rewrite_fails(monkeypatch):
    """扩写轮失败/未变长时保留原介绍，不因扩写破坏结果。"""
    fake = FakeLlm("fc_fail")
    monkeypatch.setattr(butler, "get_llm_client", lambda: fake)
    intros = butler.run_poi_intros("杭州", ["雷峰塔"])
    # FakeLlm.complete 恒返回同长版本 → 不被更短/相同内容覆盖
    assert intros["雷峰塔"] == "单轮兜底介绍。"
