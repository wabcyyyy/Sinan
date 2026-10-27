"""Two bounded official-page observations through the real active-evidence/business solver seam.

Site-specific extraction proves only these closure fields. No paid tools, model, retries or travel-ready claim.
"""

import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from unittest.mock import patch

import httpx

from app.agent.generation.content.evidence_observations import FactObservation
from app.agent.generation.orchestration import evidence_tools
from app.agent.generation.orchestration.constraint_planning import run_constraint_planning
from app.agent.generation.rules.opening_rules import parse_opening_rules
from app.agent.runtime.run_limits import current_limits
from app.schemas.active_evidence import ActiveEvidenceOptions
from app.schemas.opening_hours import OpeningHours, OpeningRule
from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip import FactEvidence, GenerateRequest
from app.schemas.trip_requirements import DayWindow, FixedArrangement, RequiredPlace, TripRequirements
from scripts.run_p4_acceptance import save

SOURCES = {
    "paris": {
        "url": "https://www.louvre.fr/zh-hans/canguanfuwu",
        "name": "卢浮宫博物馆",
        "city": "巴黎",
        "date": "2026-11-03",
        "country": "FR",
        "zone": "Europe/Paris",
        "currency": "EUR",
    },
    "tokyo": {
        "url": "https://www.tnm.jp/modules/r_free_page/index.php?id=113&lang=en",
        "name": "Tokyo National Museum",
        "city": "Tokyo",
        "date": "2026-12-15",
        "country": "JP",
        "zone": "Asia/Tokyo",
        "currency": "JPY",
    },
}


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def closure(key, text, observed):
    source = SOURCES[key]
    if source["name"] not in text:
        return None
    if key == "paris":
        match = re.search(r"除每周二闭馆外[，,]?\s*每天对外开放", text)
        if match is None:
            return None
        hours = parse_opening_rules("周二闭馆")
        hours.rules[0].source_text = match[0]
    else:
        match = re.search(
            r"December\s+(\d{1,2}),\s*(\d{4}):\s*"
            r"The museum will be closed for facility inspection and maintenance\.",
            text,
        )
        if match is None:
            return None
        specific = date(int(match[2]), 12, int(match[1]))
        hours = OpeningHours(
            status="known",
            valid_from=specific,
            valid_to=specific,
            rules=[OpeningRule(closed=True, source_text=match[0])],
        )
    return FactEvidence(
        source_url=source["url"],
        retrieved_at=observed,
        provider="night_site_specific_official_page",
        excerpt=match[0],
        verification_status="partially_verified",
        value_kind="observed",
        opening_hours=hours,
    )


def execute(root, key):
    output = root / key
    output.mkdir(parents=True, exist_ok=False)
    source = SOURCES[key]
    request = GenerateRequest(
        city=source["city"],
        days=1,
        persons=1,
        start_date=source["date"],
        requirements_struct=TripRequirements(
            constraint_planning=ConstraintPlanningOptions(
                country_code=source["country"],
                time_zone=source["zone"],
                currency=source["currency"],
            ),
            day_windows=[DayWindow(day_no=1, not_before="14:00", finish_by="18:00")],
            required_places=[RequiredPlace(constraint_id="required", name=source["name"], day_no=1)],
            fixed_arrangements=[
                FixedArrangement(
                    day_no=1,
                    name=source["name"],
                    item_type="attraction",
                    start_time="14:00",
                    end_time="16:00",
                )
            ],
        ),
    )
    context = {
        "candidates": [
            {
                "id": source["url"],
                "name": source["name"],
                "country_code": source["country"],
                "source": "official_named_entity",
                "source_url": source["url"],
                "duration_min": 120,
                "cost_currency": source["currency"],
            }
        ]
    }
    before = run_constraint_planning(request, context)
    assert request.requirements_struct is not None
    options = request.requirements_struct.constraint_planning
    assert options is not None
    options.active_evidence = ActiveEvidenceOptions(max_queries=1, budget_units=2, strategy="fixed")
    fetched = []

    def observe(gap, _problems, prepared, _request, _options):
        assert gap.kind == "opening" and gap.names == (source["name"],)
        limits = current_limits()
        if limits:
            limits.record_retrieval()
        observed = datetime.now(UTC).isoformat()
        metadata = {"url": source["url"], "observed": observed, "attempts": 1, "paid_cny": "0"}
        save(output / "http.json", metadata)
        with httpx.Client(timeout=12, follow_redirects=False) as client:
            response = client.get(source["url"])
        metadata["http_status"] = response.status_code
        save(output / "http.json", metadata)
        (output / "response.html").write_bytes(response.content)
        response.raise_for_status()
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError("official page response too large")
        parser = Text()
        parser.feed(response.text)
        text = re.sub(r"\s+", " ", " ".join(parser.parts))
        fact = closure(key, text, observed)
        fetched.append(fact)
        return FactObservation(
            "opening",
            (prepared[0].fact.source_entity_id,),
            fact=fact,
            tool="free_official_named_page",
            binding_basis="preselected_official_entity_and_page_section",
        )

    with patch.object(evidence_tools, "query_evidence", observe):
        after = run_constraint_planning(request, context)
    result = {
        "scope": "current free official HTTP + active evidence adoption + business solver; other facts unknown",
        "before": [p.model_dump(mode="json") for p in before],
        "after": [p.model_dump(mode="json") for p in after],
        "source": [f.model_dump(mode="json") if f else None for f in fetched],
        "model_calls": 0,
        "paid_calls": 0,
        "paid_cny": "0",
    }
    save(output / "result.json", result)
    return result
