"""数据集治理单测（PR-0）：三份入仓题集必须过 schema，指纹随报告走。

schema（schemas/datasets.schema.json）是题集结构的单一真源：这里既用它守住
入仓题集（改坏题集 → 红），也钉住校验器本身的拒收语义（PR-9 回流草稿的入库口）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.agent_eval.dataset_schema import (
    SCHEMA_PATH,
    DatasetError,
    dataset_kind,
    dataset_meta,
    load_dataset,
    validate_rows,
)

DATASETS = {
    "cases.json": "cases",
    "themed_cases.json": "themed_cases",
    "replay_cases.json": "replay_cases",
}


@pytest.mark.parametrize(("filename", "kind"), sorted(DATASETS.items()))
def test_committed_datasets_pass_schema(filename: str, kind: str) -> None:
    rows, meta = load_dataset(Path(__file__).with_name(filename))
    assert rows and isinstance(rows, list)
    assert meta["file"] == filename
    assert len(meta["sha256"]) == 64


def test_schema_document_is_valid_and_closes_items() -> None:
    """schema 自身可被编译；题集行级闭合（unknown 键拒收，防手滑字段）。"""
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for kind in DATASETS.values():
        Draft202012Validator.check_schema({"$ref": f"#/$defs/{kind}", "$defs": document["$defs"]})
    with pytest.raises(DatasetError, match="cases"):
        validate_rows([{"city": "杭州", "days": 1, "persons": 2, "citiy": "手滑"}], "cases")


def test_themed_case_requires_meta_fields() -> None:
    themed = {
        "name": "kyoto-themed",
        "prompt_version": "",
        "city": "京都",
        "days": 3,
        "persons": 2,
        "preferences": ["文化"],
        "intent": "巡礼",
    }
    validate_rows([themed], "themed_cases")
    bare = {key: value for key, value in themed.items() if key not in ("name", "prompt_version")}
    with pytest.raises(DatasetError, match="themed_cases"):
        validate_rows([bare], "themed_cases")


def test_out_of_contract_bounds_are_rejected() -> None:
    """题集约束对齐 GenerateRequest 契约：days 越界 / 类型不对 / 未知 kind 都拒收。"""
    base = {"city": "杭州", "days": 8, "persons": 2}
    with pytest.raises(DatasetError, match="cases"):
        validate_rows([base], "cases")
    with pytest.raises(DatasetError, match="cases"):
        validate_rows([{**base, "days": "3"}], "cases")
    with pytest.raises(DatasetError, match="replay_cases"):
        validate_rows([{"id": "x", "kind": "unknown", "description": "", "expected": ""}], "replay_cases")


def test_dataset_kind_by_filename(tmp_path: Path) -> None:
    assert dataset_kind(Path("cases.json")) == "cases"
    assert dataset_kind(Path("themed_cases.json")) == "themed_cases"
    assert dataset_kind(Path("replay_cases.json")) == "replay_cases"
    assert dataset_kind(Path("themed_extra.json")) == "themed_cases"
    assert dataset_kind(Path("whatever.json")) == "cases"


def test_dataset_meta_tracks_content(tmp_path: Path) -> None:
    first = tmp_path / "cases.json"
    first.write_text("[]", encoding="utf-8")
    second = tmp_path / "cases.json"
    meta1 = dataset_meta(first)
    first.write_text('[{"city": "杭州", "days": 1, "persons": 1}]', encoding="utf-8")
    meta2 = dataset_meta(second)
    assert meta1["sha256"] != meta2["sha256"]
    assert len(meta2["sha256"]) == 64
