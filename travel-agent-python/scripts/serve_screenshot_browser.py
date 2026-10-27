"""P3 本地浏览器验收活栈：正式 app/鉴权 + 隔离 SQLite + 真实视觉输出回放，无模型出网。"""

import argparse
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def build_app(run_dir, *, inline_worker=True):
    from app.common.config import settings

    run_dir.mkdir(parents=True, exist_ok=True)
    database = run_dir / "browser.sqlite3"
    if database.exists():
        raise ValueError("验收数据库已存在，请指定新的隔离目录；不覆盖旧数据")
    settings.jwt_secret = "example-only-p3-local-browser-signing-key-20261024"
    settings.auth_cookie_secure = False
    settings.redis_url = "redis://127.0.0.1:1/0"
    settings.nominatim_enabled = False
    settings.web_search_enabled = False
    settings.osrm_base_url = ""
    settings.ors_base_url = ""
    settings.trial_billing_enabled = False
    settings.checkpoint_db_path = str(run_dir / "checkpoints.sqlite3")
    settings.usage_db_path = str(run_dir / "usage.sqlite3")
    settings.trace_storage_path = str(run_dir / "traces.jsonl")
    settings.uploads_dir = str(run_dir / "uploads")
    settings.export_dir = str(run_dir / "exports")

    import httpx
    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.agent.editing import screenshot as vision
    from app.common.llm_client import LLMClient
    from app.db import session as db_session
    from app.db.models import Base
    from app.schemas.screenshot import ScreenshotEntity
    from app.schemas.trip import ClarifyResponse
    from app.schemas.trip_requirements import DayWindow, IntakeState, requirements_issues
    from app.services import itinerary_city, itinerary_generation, screenshot
    from app.services.image_intent import _to_data_uri
    from tests.planning_task_probe import install_inline_worker
    from tests.screenshot_cases import ROOT, kyoto_entity, resolved_entity

    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    patcher = pytest.MonkeyPatch()
    state = {
        "new_model_calls": 0,
        "blocked_model_attempts": 0,
        "blocked_http_attempts": 0,
        "vision_replays": [],
        "database": str(database),
        "scope": "real UI/API/auth/solver, replayed vision and places",
    }
    lock = threading.Lock()

    def save():
        with lock:
            (run_dir / "backend-state.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def deny_model(*_args, **_kwargs):
        state["blocked_model_attempts"] += 1
        save()
        raise RuntimeError("P3 browser acceptance: new model calls forbidden")

    def deny_http(*_args, **_kwargs):
        state["blocked_http_attempts"] += 1
        save()
        raise RuntimeError("P3 browser acceptance: backend external HTTP forbidden")

    patcher.setattr(LLMClient, "chat_response", deny_model)
    patcher.setattr(LLMClient, "stream_chat_deltas", deny_model)
    patcher.setattr(httpx.HTTPTransport, "handle_request", deny_http)
    recorded = json.loads((ROOT / "real_vision_20261024.json").read_text(encoding="utf-8-sig"))
    by_uri = {_to_data_uri((ROOT / f"{r['fixture']}.png").read_bytes()): r for r in recorded["results"]}

    class RecordedVision:
        model = "deepseek-flash"
        base_url = "https://api.deepseek.com"

        def chat(self, messages, **_kwargs):
            uri = messages[0]["content"][0]["image_url"]["url"]
            row = by_uri.get(uri)
            if row is None:
                raise ValueError("仅允许复用四张授权图片的原输出")
            state["vision_replays"].append(row["fixture"])
            save()
            return json.dumps(row["extraction"], ensure_ascii=False)

    patcher.setattr(vision, "get_llm_client", lambda _role: RecordedVision())

    def resolve(name, _city, country, _kind="place"):
        if country == "FR":
            return [resolved_entity()]
        if country == "JP":
            if "駅" in name or "station" in name.lower():
                observation = json.loads(
                    (ROOT / "kyoto_station_observation_20261024.json").read_text(encoding="utf-8-sig")
                )
                return [ScreenshotEntity.model_validate(row) for row in observation["adapter_choices"]]
            return [kyoto_entity()]
        return []

    patcher.setattr(screenshot, "resolve_screenshot_place", resolve)

    def clarify(request):
        # 冷启动槽位是场景输入；复核保留浏览器带回的原需求，绝不编写正式行程。
        current = (
            request.state.model_copy(deep=True)
            if request.state
            else IntakeState(
                city="京都" if "京都" in request.message else "Paris",
                days=1,
                persons=1,
                start_date="2026-10-25",
                stay_nights=0,
            )
        )
        for key, value in request.slots.items():
            if (
                key in {"city", "days", "persons", "start_date", "stay_nights", "budget", "preferences"}
                and value is not None
            ):
                setattr(current, key, value)
        if not current.requirements.day_windows:
            current.requirements.day_windows = [
                DayWindow(day_no=1, kind="arrival", not_before="14:00", finish_by="16:00")
            ]
        issues = requirements_issues(current.requirements, days=current.days)
        slots = {
            k: getattr(current, k) for k in ("city", "days", "persons", "start_date", "stay_nights", "preferences")
        }
        return ClarifyResponse(
            state=current,
            slots=slots,
            ready=not issues,
            blocked=bool(issues),
            reply="；".join(issues) if issues else "本地验收槽位已确认，请核对截图需求。",
            next="confirm",
        )

    patcher.setattr(itinerary_city, "run_clarify", clarify)
    patcher.setattr(itinerary_generation, "run_plan_context", lambda *_args, **_kwargs: {"candidates": []})
    patcher.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args))
    patcher.setattr(itinerary_generation.enricher_pool, "submit", lambda task, *args: task(*args))
    patcher.setattr(itinerary_generation, "_submit_budget_recalculate", lambda _identifier: None)
    if inline_worker:
        install_inline_worker(patcher)
    save()

    # 复用正式路由和 middleware；lifespan 关闭，避免正式运维定时器与数据库迁移。
    from main import app

    app.state.p3_patcher = patcher
    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18073)
    args = parser.parse_args()
    uvicorn.run(
        build_app(args.run_dir.resolve()), host="127.0.0.1", port=args.port, lifespan="off", log_level="warning"
    )


if __name__ == "__main__":
    main()
