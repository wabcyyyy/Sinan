"""M3-②（AD5）意图关键词抽词补池与管家/点位介绍扩写的回归测试。

覆盖：
- build_intent_keywords：书名号/引号优先、停用词过滤、空值、上限 8、去重；
- supervisor.decompose：三域任务卡 intent_keywords 填充一致；
- evaluate_research：意图覆盖确定性检查（全不命中→不足；命中→维持原判定；
  关键词为空→与旧行为完全一致；仅第 1 轮生效）；
- factory._run_search：task.intent_keywords 并入补池 extras（去重、顺序），
  并透传给联网补池 search_places_via_web；
- web_search：联网补池 query 逐词追加「city + keyword」；
- butler/poi Prompt 扩写契约（400~600 四段 / 200~300 + 意图连接句）。
"""

import json

from app.agent.core.intent import build_intent_keywords
from app.agent.data import web_search
from app.agent.generation.content import butler
from app.agent.research import reasoning
from app.agent.research.evidence import ResearchTask
from app.agent.research.factory import run_research
from app.agent.research.supervisor import decompose
from app.agent.tools import impl as tools
from app.agent.tools.function_calling import FunctionCallingError
from app.common.config import settings
from app.schemas.trip import GenerateRequest

# ---------- 1. build_intent_keywords：纯规则抽词 ----------


def test_build_intent_keywords_extracts_work_and_brand_terms():
    """《千恋万花》圣地巡礼，住柏悦，节奏松弛 → 作品名优先、品牌词剥前缀、无停用词。"""
    kws = build_intent_keywords("《千恋万花》圣地巡礼，住柏悦，节奏松弛")
    assert kws[0] == "千恋万花"  # 书名号内容（作品名）优先
    assert "圣地巡礼" in kws
    assert any("柏悦" in kw for kw in kws)  # 「住柏悦」剥离动词首字后命中品牌词
    assert "节奏" not in kws and "旅游" not in kws and "酒店" not in kws  # 停用词不进池


def test_build_intent_keywords_quoted_priority_and_dedup():
    kws = build_intent_keywords('"环球影城" 西湖 环球影城 《环球影城》')
    assert kws[0] == "环球影城"  # 引号内容优先成词
    assert kws.count("环球影城") == 1  # 书名号/引号/切词结果去重
    assert "西湖" in kws


def test_build_intent_keywords_empty_returns_empty():
    assert build_intent_keywords(None) == []
    assert build_intent_keywords("") == []
    assert build_intent_keywords("   ") == []
    assert build_intent_keywords("去") == []  # 单字无检索价值


def test_build_intent_keywords_cap_eight():
    intent = "迪士尼 环球影城 长隆 海洋公园 泡温泉 爬黄山 逛故宫 看洱海 去大理"
    kws = build_intent_keywords(intent)
    assert len(kws) == 8  # 上限 8
    assert kws[0] == "迪士尼"


# ---------- 2. supervisor 填充：三域任务卡 intent_keywords 一致 ----------


def test_decompose_fills_intent_keywords_for_all_domains():
    req = GenerateRequest(city="杭州", days=2, intent="《千恋万花》圣地巡礼，住柏悦")
    expected = build_intent_keywords(req.intent)
    assert expected  # 抽词非空
    tasks = decompose(req)
    assert [t.domain for t in tasks] == ["attraction", "food", "hotel"]
    assert all(t.intent_keywords == expected for t in tasks)
    assert all(t.intent == req.intent for t in tasks)


def test_decompose_without_intent_keeps_empty_keywords():
    tasks = decompose(GenerateRequest(city="杭州", days=1))
    assert all(t.intent_keywords == [] for t in tasks)


# ---------- 3. evaluate_research：意图覆盖确定性维度 ----------


def test_evaluate_intent_uncovered_forces_insufficient(monkeypatch):
    """第 1 轮证据完全未命中意图词 → 确定性判定不足，补充词含意图关键词。"""
    monkeypatch.setattr(settings, "llm_api_key", "configured")
    task = ResearchTask(domain="attraction", city="杭州", intent_keywords=["千恋万花", "圣地巡礼"])
    verdict = reasoning.evaluate_research(task, [{"name": "西湖", "remark": "湖光山色"}], 1)
    assert verdict["sufficient"] is False
    assert "千恋万花" in verdict["extra_keywords"]
    assert "圣地巡礼" in verdict["extra_keywords"]


def test_evaluate_intent_hit_via_tags_keeps_llm_verdict(monkeypatch):
    """tags 命中意图词 → 不短路，维持 LLM 判定链路。"""

    class FakeEvalClient:
        def complete(self, *_args, **_kwargs):
            return json.dumps({"sufficient": True, "reason": "ok", "extra_keywords": []}, ensure_ascii=False)

    monkeypatch.setattr(settings, "llm_api_key", "configured")
    monkeypatch.setattr(reasoning, "get_llm_client", lambda: FakeEvalClient())
    task = ResearchTask(domain="attraction", city="杭州", intent_keywords=["千恋万花"])
    verdict = reasoning.evaluate_research(task, [{"name": "动漫画馆", "tags": ["千恋万花 联动展览"]}], 1)
    assert verdict["sufficient"] is True
    assert verdict["extra_keywords"] == []


def test_evaluate_empty_intent_keywords_keeps_legacy_behavior(monkeypatch):
    """intent_keywords 为空 → 行为与旧版完全一致（未配置 LLM 时直接判充分）。"""
    monkeypatch.setattr(settings, "llm_api_key", "")
    task = ResearchTask(domain="food", city="杭州")
    verdict = reasoning.evaluate_research(task, [{"name": "楼外楼"}], 1)
    assert verdict == {"sufficient": True, "extra_keywords": []}


def test_evaluate_intent_check_only_on_round_one(monkeypatch):
    """意图覆盖检查仅第 1 轮生效：第 2 轮不再短路（保证补查轮可收敛）。"""
    monkeypatch.setattr(settings, "llm_api_key", "")
    task = ResearchTask(domain="attraction", city="杭州", intent_keywords=["千恋万花"])
    verdict = reasoning.evaluate_research(task, [{"name": "西湖"}], 2)
    assert verdict == {"sufficient": True, "extra_keywords": []}


# ---------- 4. factory._run_search：intent_keywords 并入补池 extras ----------


def test_run_search_merges_task_intent_keywords_into_extras(monkeypatch):
    """task.intent_keywords 追加进 extras 补池链：plan 词先行、dict.fromkeys 去重。"""
    captured = {"keywords": []}

    def fake_search(city, preferences=None, limit=30):
        # ≥3 条避免触发联网补池分支
        return [{"name": f"{city}景点{i}", "latitude": 30.0, "longitude": 120.0} for i in range(4)]

    import app.agent.data.web_search as web_search_mod

    def fake_web(city, category, limit=4, intent_keywords=None):
        for keyword in intent_keywords or []:
            captured["keywords"].append(keyword)
        return []

    monkeypatch.setattr(tools, "search_attractions", fake_search)
    monkeypatch.setattr(web_search_mod, "search_places_via_web", fake_web)
    # 补池闸门整体桩成开：生产闸门还要看 search 角色凭据与 addon（机器状态，
    # CI 上凭据恒假），只靠默认 settings 会让本用例在 CI 假红。
    monkeypatch.setattr(web_search_mod, "web_search_enabled", lambda: True)
    monkeypatch.setattr(reasoning, "plan_research", lambda task: {"extra_keywords": ["杭州 西湖"]})
    monkeypatch.setattr(
        reasoning, "evaluate_research", lambda task, items, round_no: {"sufficient": True, "extra_keywords": []}
    )
    task = ResearchTask(domain="attraction", city="杭州", intent_keywords=["杭州 千恋万花", "杭州 西湖"])
    pack = run_research(task)
    assert captured["keywords"][0] == "杭州 西湖"  # plan 补充词顺序不变
    assert "杭州 千恋万花" in captured["keywords"]  # 意图关键词并入补池
    assert captured["keywords"].count("杭州 西湖") == 1  # 去重（web 补池由 factory 去重后的 extras 驱动）
    assert pack.rounds == 1


def test_run_search_passes_intent_keywords_to_web_refill(monkeypatch):
    """证据不足触发联网补池时，task.intent_keywords 透传给 search_places_via_web。"""
    captured = {}

    def fake_search(city, preferences=None, limit=30):
        return []  # 少于 3 条 → 触发联网补池

    def fake_web(city, category, limit=4, **kwargs):
        captured.update({"city": city, "category": category, "limit": limit, **kwargs})
        return []

    monkeypatch.setattr(tools, "search_attractions", fake_search)
    monkeypatch.setattr(web_search, "web_search_enabled", lambda: True)
    monkeypatch.setattr(web_search, "search_places_via_web", fake_web)
    monkeypatch.setattr(reasoning, "plan_research", lambda task: {})
    monkeypatch.setattr(
        reasoning, "evaluate_research", lambda task, items, round_no: {"sufficient": True, "extra_keywords": []}
    )
    task = ResearchTask(domain="attraction", city="杭州", intent_keywords=["千恋万花"])
    run_research(task)
    assert captured["city"] == "杭州"
    assert captured["intent_keywords"] == ["千恋万花"]


# ---------- 5. web 补池 query：逐词追加 city + keyword ----------


def test_search_places_via_web_appends_intent_keywords(monkeypatch):
    captured = {}

    def fake_json(question, *, schema_hint, max_tokens=800):
        captured["question"] = question
        return {"items": [{"name": "千恋万花主题馆", "intro": "巡礼点"}]}

    monkeypatch.setattr(web_search, "web_search_enabled", lambda: True)
    monkeypatch.setattr(web_search, "search_channel_ready", lambda: True)  # 本用例验 query 拼装，与通道能力无关
    monkeypatch.setattr(web_search, "web_search_json", fake_json)
    rows = web_search.search_places_via_web("杭州", "attraction", intent_keywords=["千恋万花", "圣地巡礼"])
    assert rows and rows[0]["name"] == "千恋万花主题馆"
    question = captured["question"]
    assert "杭州 千恋万花" in question and "杭州 圣地巡礼" in question  # city+keyword 逐词
    assert "有哪些" in question  # 既有 query 规则保留


def test_search_places_via_web_without_intent_keeps_query(monkeypatch):
    captured = {}

    def fake_json(question, *, schema_hint, max_tokens=800):
        captured["question"] = question
        return {"items": []}

    monkeypatch.setattr(web_search, "web_search_enabled", lambda: True)
    monkeypatch.setattr(web_search, "search_channel_ready", lambda: True)  # 本用例验 query 拼装，与通道能力无关
    monkeypatch.setattr(web_search, "web_search_json", fake_json)
    web_search.search_places_via_web("杭州", "attraction")
    assert "意图" not in captured["question"]  # 无意图词时 query 不变


# ---------- 6. butler / poi Prompt 扩写契约 ----------


class CaptureClient:
    """捕获 complete 入参的 LLM 替身；reply 可定制。"""

    def __init__(self, reply="第一段。\n\n第二段。"):
        self.reply = reply
        self.calls = []

    def complete(self, user_prompt, system_prompt="", **kwargs):
        self.calls.append({"user": user_prompt, "system": system_prompt, **kwargs})
        return self.reply


def test_butler_note_prompt_four_paragraph_contract(monkeypatch):
    """管家讲解：400~600 字四段结构 + intent/plans 注入 + max_tokens 1200。"""
    fake = CaptureClient()
    monkeypatch.setattr(butler, "get_llm_client", lambda: fake)
    butler.run_butler_note(
        {
            "city": "杭州",
            "days": 2,
            "persons": 2,
            "preferences": ["亲子"],
            "intent": "《千恋万花》圣地巡礼",
            "plans": [{"day_no": 1, "items": ["西湖"]}],
        }
    )
    call = fake.calls[0]
    assert call["max_tokens"] == 1200
    assert "400" in call["system"] and "600" in call["system"]
    assert "四段" in call["system"]
    assert "酒店选址" in call["system"] and "预算" in call["system"]
    assert "markdown" in call["system"]  # 既有格式约束保留
    assert "《千恋万花》圣地巡礼" in call["user"]  # intent 注入点
    assert "西湖" in call["user"]  # plans 注入点


def test_poi_intros_prompt_length_and_intent_link(monkeypatch):
    """点位介绍：200~300 字三段式 + 意图连接句要求 + max_tokens 按量上调。"""
    fake = CaptureClient(reply=json.dumps({"intros": {"西湖": "介绍"}}, ensure_ascii=False))

    def _fc_fail(*_args, **_kwargs):
        raise FunctionCallingError("测试强制降级")

    monkeypatch.setattr(butler, "get_llm_client", lambda: fake)
    monkeypatch.setattr(butler, "run_tool_call_loop", _fc_fail)
    butler.run_poi_intros("杭州", ["西湖"], intent="《千恋万花》圣地巡礼")
    call = fake.calls[0]
    # 200-300 字/地点：max_tokens 按地点数上调（1 点位 → 2600 地板）
    assert call["max_tokens"] == 2600
    assert "200~300" in call["system"]
    assert "《千恋万花》圣地巡礼" in call["system"]  # 意图连接句要求注入
    assert "连接" in call["system"]


def test_poi_intros_prompt_without_intent_uses_reputation_link(monkeypatch):
    """intent 为空时连接句降级为口碑/地理理由。"""
    fake = CaptureClient(reply=json.dumps({"intros": {"西湖": "介绍"}}, ensure_ascii=False))

    def _fc_fail(*_args, **_kwargs):
        raise FunctionCallingError("测试强制降级")

    monkeypatch.setattr(butler, "get_llm_client", lambda: fake)
    monkeypatch.setattr(butler, "run_tool_call_loop", _fc_fail)
    butler.run_poi_intros("杭州", ["西湖"])
    system = fake.calls[0]["system"]
    assert "200~300" in system
    assert "口碑" in system and "地理" in system
    assert "旅行意图" not in system  # 无意图时不出现意图连接要求
