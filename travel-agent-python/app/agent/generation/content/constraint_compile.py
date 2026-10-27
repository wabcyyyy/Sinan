"""单日 CP-SAT 结构：可选活动 circuit、静态有向边、当地分钟及可解释假设。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from app.agent.core.poi_identity import norm_ws_key
from app.agent.generation.content.constraint_problem import DayPlanningProblem, minute
from app.agent.generation.content.constraint_requirements import (
    compile_budget,
    compile_fixed,
    compile_places,
    compile_relations,
    unsupported_requirements,
)
from app.schemas.constraints import ConstraintCheck, ConstraintKind


@dataclass
class CompiledDay:
    problem: DayPlanningProblem
    model: cp_model.CpModel = field(default_factory=cp_model.CpModel)
    selected: list[cp_model.IntVar] = field(default_factory=list)
    starts: list[cp_model.IntVar] = field(default_factory=list)
    arcs: dict[tuple[int, int], cp_model.IntVar] = field(default_factory=dict)
    route_choices: dict[tuple[int, int, int], cp_model.IntVar] = field(default_factory=dict)
    prices: list[int | None] = field(default_factory=list)
    assumptions: dict[int, ConstraintCheck] = field(default_factory=dict)
    gaps: list[ConstraintCheck] = field(default_factory=list)
    objectives: list = field(default_factory=list)

    def hard(self, key: str, kind: ConstraintKind, reason: str, refs=(), evidence=()):
        literal = self.model.new_bool_var(key)
        self.model.add_assumption(literal)
        self.assumptions[literal.index] = ConstraintCheck(
            constraint_id=key,
            kind=kind,
            day_no=self.problem.day_no,
            status="unknown",
            blocking=True,
            reason=reason,
            item_refs=list(refs),
            evidence_refs=list(evidence),
        )
        return literal

    def gap(self, key: str, reason: str, refs=()):
        self.gaps.append(
            ConstraintCheck(
                constraint_id=key,
                kind="planning_evidence",
                day_no=self.problem.day_no,
                status="unknown",
                reason=reason,
                item_refs=list(refs),
                blocking=True,
            )
        )

    def matches(self, name: str) -> list[int]:
        found = [
            i
            for i, c in enumerate(self.problem.candidates)
            if norm_ws_key(name) in {norm_ws_key(value) for value in (c.name, *c.aliases)}
        ]
        same_source_entity = (
            self.problem.requirements.fact_source == "chinatravel"
            and len(
                {
                    (self.problem.candidates[i].name, self.problem.candidates[i].candidate_id.rsplit(":", 1)[-1])
                    for i in found
                }
            )
            == 1
        )
        if len(found) > 1 and not same_source_entity:
            self.gap(f"entity:{name}", "同名候选未消歧，当前只作乐观试解", [name])
        return found


def _routes(compiled: CompiledDay):
    model, candidates = compiled.model, compiled.problem.candidates
    empty = model.new_bool_var("empty")
    circuit: list[tuple[int, int, cp_model.LiteralT]] = [(0, 0, empty)]
    model.add(sum(compiled.selected) == 0).only_enforce_if(empty)
    model.add(sum(compiled.selected) >= 1).only_enforce_if(empty.Not())
    travel_terms = []
    for i, candidate in enumerate(candidates):
        selected, start = compiled.selected[i], compiled.starts[i]
        model.add(start == 0).only_enforce_if(selected.Not())
        model.add(start + candidate.duration_min <= 1440).only_enforce_if(selected)
        circuit.append((i + 1, i + 1, selected.Not()))
        circuit.extend((a, b, model.new_bool_var(f"arc:{a}:{b}")) for a, b in ((0, i + 1), (i + 1, 0)))
        for j, target in enumerate(candidates):
            if i == j:
                continue
            arc = model.new_bool_var(f"arc:{i + 1}:{j + 1}")
            compiled.arcs[i, j] = arc
            circuit.append((i + 1, j + 1, arc))
            route = compiled.problem.route_matrix.get((candidate.candidate_id, target.candidate_id))
            duration = route["duration_min"] if route else 0
            evidence = [str(route["source"])] if route else ["missing-route:optimistic-lower-bound"]
            guard = compiled.hard(
                f"route:{candidate.candidate_id}:{target.candidate_id}",
                "time_conflict",
                f"该有向转场至少需 {duration} 分钟",
                [candidate.name, target.name],
                evidence,
            )
            model.add(compiled.starts[j] >= start + candidate.duration_min + duration).only_enforce_if([arc, guard])
            if route and route.get("options"):
                variants = []
                for k, option in enumerate(route["options"]):
                    choice = model.new_bool_var(f"route-option:{i}:{j}:{k}")
                    compiled.route_choices[i, j, k] = choice
                    variants.append(choice)
                    model.add(
                        compiled.starts[j] >= start + candidate.duration_min + option["duration_min"]
                    ).only_enforce_if([choice, guard])
                    travel_terms.append(option["duration_min"] * choice)
                model.add(sum(variants) == arc)
            else:
                travel_terms.append(duration * arc)
    model.add_circuit(circuit)
    return -sum(travel_terms)


def _windows(compiled: CompiledDay) -> None:
    problem, model = compiled.problem, compiled.model
    windows = [w for w in problem.requirements.day_windows if w.day_no == problem.day_no]
    for k, window in enumerate(windows or [None]):
        relative = problem.requirements.fact_source == "chinatravel"
        low = minute(window.not_before) if window and window.not_before else (0 if relative else 480)
        high = minute(window.finish_by) if window and window.finish_by else (1440 if relative else 1200)
        evidence = [f"turn:{window.source_turn_id}"] if window and window.source_turn_id else []
        guard = compiled.hard(
            f"day-window:{problem.day_no}:{k}",
            "day_window",
            f"当地日窗 {low}–{high} 分钟；官方相对日用自然日边界，其余沿用原型默认",
            evidence=evidence,
        )
        for i, candidate in enumerate(problem.candidates):
            model.add(compiled.starts[i] >= low).only_enforce_if([compiled.selected[i], guard])
            model.add(compiled.starts[i] + candidate.duration_min <= high).only_enforce_if(
                [compiled.selected[i], guard]
            )
    for i, candidate in enumerate(problem.candidates):
        if candidate.opening_windows is None:
            continue
        guard = compiled.hard(
            f"opening:{candidate.candidate_id}",
            "time_conflict",
            "访问须完整落在同一营业窗内",
            [candidate.name],
            [candidate.source],
        )
        choices = [model.new_bool_var(f"opening:{i}:{k}") for k in range(len(candidate.opening_windows))]
        model.add(sum(choices) == compiled.selected[i]).only_enforce_if(guard)
        for choice, (low, high) in zip(choices, candidate.opening_windows, strict=True):
            model.add(compiled.starts[i] >= low).only_enforce_if([choice, guard])
            model.add(compiled.starts[i] + candidate.duration_min <= high).only_enforce_if([choice, guard])


def compile_day(problem: DayPlanningProblem, *, model: cp_model.CpModel | None = None) -> CompiledDay:
    compiled = CompiledDay(problem, model=model if model is not None else cp_model.CpModel())
    compiled.selected = [compiled.model.new_bool_var(f"visit:{c.candidate_id}") for c in problem.candidates]
    compiled.starts = [compiled.model.new_int_var(0, 1439, f"start:{c.candidate_id}") for c in problem.candidates]
    travel_objective = _routes(compiled)
    _windows(compiled)
    compile_places(compiled)
    compile_fixed(compiled)
    compile_budget(compiled)
    soft_objectives = compile_relations(compiled)
    unsupported_requirements(compiled)
    compiled.objectives = [
        *soft_objectives,
        sum(c.utility * compiled.selected[i] for i, c in enumerate(problem.candidates)),
        travel_objective,
        -sum(compiled.starts),
    ]
    return compiled
