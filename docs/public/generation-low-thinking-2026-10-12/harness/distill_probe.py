"""Measure distill_intent under global low thinking with the production budget (no edits)."""
import json, sys
from common import CallLog, strip_content
from app.agent.core import intent as intent_mod
log = CallLog(); log.install()
rows = []
for text in ["东京2天，2个人，预算3000元人民币，带70岁老人，慢节奏，不去寺庙，每天最多两个景点，素食可吃蛋奶。",
             "京都 3 日《千恋万花》圣地巡礼，住柏悦，节奏松弛",
             "带 5 岁娃的杭州一日，以动物和自然科学为主，节奏松弛"]:
    log.entry = text[:12]
    brief = intent_mod.distill_intent(text)
    c = strip_content(log.calls[-1:])[0]
    c["brief_ok"] = brief is not None
    rows.append(c); print(json.dumps(c, ensure_ascii=False), flush=True)
open(f"distill-{sys.argv[1]}.json", "w", encoding="utf-8").write(json.dumps(rows, ensure_ascii=False, indent=2))
