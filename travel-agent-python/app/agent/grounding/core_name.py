"""描述性地点名 → 可检索的核心场所名（fast 模型，一次小调用）。

存在性解析的查询失败大头是"描述性全串"（"新宿御苑前 ソラノイロ"0 行，短名
命中同一实体；"明治神宫外苑银杏并木"同理——实测 2026-10-11）。AI-native 数据面
（AGENTS.md）：本地不存语料，名称整理交给模型世界知识；抽取结果仍要过存在性
判定的名称门槛（same_entity），抽错实体进不了判定，最坏多一次 1rps 查询。

未配置 fast 模型（离线 eval/CI）整体短路——重试路径不存在，行为与不抽取一致。
失败/超时/解析不出都按空串处理，绝不阻塞或伪造。负缓存是进程级有界表：
UNKNOWN 判定不进 resolve 记忆，修复循环会对同一名字反复重解析，没有它每次
都会白付一次 LLM 抽取。

依赖：common（llm_client/model_registry）；无 agent 内部依赖（existence 消费）。
"""

from __future__ import annotations

import json

from app.common.llm_client import get_role_client
from app.common.model_registry import configured, model_for

_CACHE_MAX = 256
_cache: dict[str, str] = {}

_SYSTEM = (
    "你是地名整理助手。输入一段可能带位置前缀、餐食描述或景点注解的地点文字，"
    "提取其中指代具体真实场所（餐厅/景点/建筑/设施）的核心名称，保持原文语言与写法。"
    "若文字描述的是一顿饭或一类服务而不是具体场所，返回空字符串。"
    '只输出 JSON：{"core": "核心名称"}，不要输出其他内容。'
)


def core_name_via_llm(name: str, city: str) -> str:
    """描述性地点名 → 核心场所名；未配置/失败返回空串。"""
    key = f"{str(city or '').strip()}|{str(name or '').strip()}"
    cached = _cache.get(key)
    if cached is not None:
        return cached
    core = _extract(name, city)
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = core
    return core


def _extract(name: str, city: str) -> str:
    if not configured("fast"):
        return ""
    try:
        client = get_role_client("fast")
        raw = client.complete(
            f"目的地：{city}\n地点文字：{name}",
            system_prompt=_SYSTEM,
            temperature=0.0,
            # 思考（全局 low）计入 completion：原 60 档实测 4/4 被思考吃满、正文为空
            # （finish_reason=length）。2026-10-12 六个真实名字实测 completion 75-558。
            max_tokens=1024,
            model=model_for("fast"),
            json_mode=True,
        )
        parsed = json.loads(str(raw or "").strip().strip("`"))
        candidate = str((parsed or {}).get("core") or "").strip()
        # 抽取必须比原名短（去前缀/注解才有检索意义）且不是城市名本身
        if candidate and len(candidate) < len(name) and candidate not in str(city or ""):
            return candidate
    except Exception:
        return ""
    return ""


def reset_core_name_cache() -> None:
    """测试/评测用：清空抽取负缓存（与 existence.reset_existence_state 同场景）。"""
    _cache.clear()
