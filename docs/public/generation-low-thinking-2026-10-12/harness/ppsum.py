import json, sys
sys.stdout.reconfigure(encoding="utf-8")
for r in json.load(open(sys.argv[1], encoding="utf-8")):
    if "replayError" in r:
        print(r["file"].split("/")[-1], "REPLAY ERROR", r["replayError"]); continue
    days = [(d["dayNo"], [m["name"] + ("+" if m["diet"] else "-") for m in d["finalMeals"]], len(d["finalAttractions"]), d["removed"]) for d in r["days"]]
    print(r["file"].split("/")[-1], r["variant"], "PASS" if r["rulePass"] else "FAIL", days, [i[:70] for i in r["issues"]])
