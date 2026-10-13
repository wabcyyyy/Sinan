import json, sys
sys.stdout.reconfigure(encoding="utf-8")
for line in open(sys.argv[1], encoding="utf-8"):
    r = json.loads(line)
    evs = r.get("events") or []
    llm = [e for e in evs if e.get("name") in ("llm.request", "llm.stream_request")]
    lim = next((e.get("metadata") for e in evs if e.get("name") == "run_limits"), {}) or {}
    st = next((e for e in evs if e.get("name") == "run_status"), None)
    print("RUN", r.get("request_id"), "elapsed_s", round((lim.get("elapsed_ms") or 0) / 1000, 1), "llm_calls", lim.get("llm_calls"), "tokens", (lim.get("prompt_tokens") or 0) + (lim.get("completion_tokens") or 0), "status", (st or {}).get("error", "")[:120] if st else "ok")
    for e in llm:
        m = e.get("metadata") or {}
        print("   ", e["name"].split(".")[-1], "max?", "fin=", m.get("finish_reason"), "comp=", m.get("completion_tokens"), "reason=", m.get("reasoning_tokens"), "chars=", m.get("content_chars", m.get("content_length")))
    drops = [e.get("metadata") for e in evs if e.get("name") == "duplicate_dropped"]
    if drops: print("    drops", drops)
