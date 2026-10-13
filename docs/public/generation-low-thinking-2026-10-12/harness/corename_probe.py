"""Measure core_name extraction reasoning/completion under global low thinking at a given max_tokens."""
import json, sys, time
from common import CallLog, strip_content
from app.agent.grounding import core_name
log = CallLog(); log.install()
budget = int(sys.argv[1])
orig = core_name._extract
names = ["新宿御苑前 ソラノイロ", "明治神宫外苑银杏并木", "AFURI 新宿店", "Naritaya 东京站", "新宿站西口素食咖啡店（豆乳甜品与轻食）", "菜食健美 新宿本店"]
src = open(core_name.__file__, encoding="utf-8").read()
assert "max_tokens=60" in src or f"max_tokens={budget}" in src or "_MAX_TOKENS" in src
from app.common.llm_client import get_role_client
from app.common.model_registry import model_for
rows = []
for n in names:
    log.entry = n
    t = time.monotonic()
    raw = get_role_client("fast").complete(f"目的地：东京\n地点文字：{n}", system_prompt=core_name._SYSTEM, temperature=0.0, max_tokens=budget, model=model_for("fast"), json_mode=True)
    c = strip_content(log.calls[-1:])[0]
    c["raw"] = raw
    rows.append(c); print(json.dumps(c, ensure_ascii=False), flush=True)
open(f"corename-{budget}.json", "w", encoding="utf-8").write(json.dumps(rows, ensure_ascii=False, indent=2))
