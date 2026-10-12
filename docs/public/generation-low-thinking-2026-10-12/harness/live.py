"""Real end-to-end Tokyo strict/candidate generation against the isolated backend on :8004.

Usage: python live.py <label> [strict|candidate ...]
Writes lowthink/live-<label>-<policy>.json; prints per-day final (post-processed, persisted) items.
"""

import copy
import json
import sys
import time
from pathlib import Path

import httpx

here = Path(__file__).parent
audit = here.parent
label = sys.argv[1]
policies = sys.argv[2:] or ["strict", "candidate"]
account = json.loads((audit / "test-account.json").read_text(encoding="utf-8"))
base_input = json.loads((audit / "generation-submissions.json").read_text(encoding="utf-8"))["input"]
MESSAGE = "东京2天，2个人，预算3000元人民币，带70岁老人，慢节奏，不去寺庙，每天最多两个景点，素食可吃蛋奶。"
CHOICE = {"candidate": "先给餐饮候选，我会自行核实", "strict": "只安排有饮食依据的餐厅"}
sys.stdout.reconfigure(encoding="utf-8")


def summarize(trip: dict) -> dict:
    days = []
    for day in trip["dayList"]:
        items = []
        for item in day["items"]:
            diet = sorted(k for k in (item.get("factEvidence") or {}) if k.startswith("diet:"))
            items.append(
                {
                    "type": item["itemType"],
                    "name": item["poiName"],
                    "time": f"{item.get('startTime')}-{item.get('endTime')}",
                    "poiId": item.get("poiId"),
                    "diet": diet,
                }
            )
        meals = [i for i in items if i["type"] == "food"]
        days.append(
            {
                "dayNo": day["dayNo"],
                "status": day["generationStatus"],
                "error": day.get("generationError"),
                "attractions": sum(i["type"] == "attraction" for i in items),
                "meals": len(meals),
                "mealsWithDiet": sum(1 for m in meals if m["diet"]),
                "items": items,
            }
        )
    return {"id": trip["id"], "genState": trip["genState"], "quality": trip["qualityStatus"], "days": days}


with httpx.Client(base_url="http://127.0.0.1:8004", timeout=300) as client:
    assert client.post("/api/auth/login", json=account).json()["code"] == 200
    extractions = []
    for attempt in range(3):
        t0 = time.monotonic()
        data = client.post("/api/itinerary/clarify", json={"message": MESSAGE}).json()["data"]
        extractions.append({"seconds": round(time.monotonic() - t0, 2), "data": data})
        req = data["state"]["requirements"]
        ok = (
            req["maxAttractionsPerDay"] == 2
            and req["dietaryRequirement"] == "vegetarian"
            and "temple" in req["excludedCategories"]
        )
        print("extraction", attempt, "degraded", data.get("degraded"), "ok", ok, extractions[-1]["seconds"], flush=True)
        if ok:
            break
    (here / f"live-{label}-extractions.json").write_text(json.dumps(extractions, ensure_ascii=False, indent=2), encoding="utf-8")
    assert ok, "extraction failed 3x; see saved responses"
    for policy in policies:
        ready = client.post("/api/itinerary/clarify", json={"message": CHOICE[policy], "state": copy.deepcopy(data["state"])}).json()["data"]
        for _ in range(3):
            if ready["ready"]:
                break
            reply = {"start_date": "日期待定", "preferences": "特色美食 · 慢节奏", "budget": "全程预算3000元"}.get(ready.get("next"))
            assert reply, ready
            ready = client.post("/api/itinerary/clarify", json={"message": reply, "state": ready["state"]}).json()["data"]
        assert ready["ready"], ready
        payload = copy.deepcopy(base_input)
        payload["requirementsStruct"] = ready["state"]["requirements"]
        start = time.monotonic()
        trip_id = client.post("/api/itinerary/generate", json=payload).json()["data"]["id"]
        while time.monotonic() - start < 480:
            trip = client.get(f"/api/itinerary/{trip_id}").json()["data"]
            if trip["status"] in (2, 3):
                break
            time.sleep(3)
        seconds = round(time.monotonic() - start, 2)
        summary = summarize(trip)
        record = {"label": label, "case": policy, "tripId": trip_id, "seconds": seconds, "summary": summary, "input": payload, "body": trip}
        (here / f"live-{label}-{policy}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(policy, trip_id, summary["genState"], summary["quality"], f"{seconds}s", flush=True)
        for day in summary["days"]:
            print(f"  day{day['dayNo']} {day['status']} attr={day['attractions']} meals={day['meals']} diet={day['mealsWithDiet']} err={(day['error'] or '')[:160]}", flush=True)
            for item in day["items"]:
                print(f"     {item['time']} {item['type']:<10} {item['name']} {','.join(item['diet'])}", flush=True)
