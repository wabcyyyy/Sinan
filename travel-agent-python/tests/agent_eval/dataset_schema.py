"""评测数据集治理（PR-0）：JSON Schema 校验 + 数据集指纹。

三个数据集（cases.json / themed_cases.json / replay_cases.json）在**加载点**统一过
`schemas/datasets.schema.json`（单一真源，Draft 2020-12）：错题在出题文件里就被拦下，
不会跑到指标层才变成一个看不懂的低分。报告记录数据集指纹（文件名 + 内容 SHA256），
评测结论因此可回溯到"哪份题"——此前报告只记 prompt_version，改题集与改行为分不开。

`validate_rows` 同时是 PR-9 回流草稿的入库前校验口（计划 §PR-9 验收）：
草稿只有过了同一份 schema 才准进 replay_cases.json。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

DatasetKind = Literal["cases", "themed_cases", "replay_cases"]

SCHEMA_PATH = Path(__file__).with_name("schemas") / "datasets.schema.json"

_BY_FILENAME: dict[str, DatasetKind] = {
    "cases.json": "cases",
    "themed_cases.json": "themed_cases",
    "replay_cases.json": "replay_cases",
}


class DatasetError(ValueError):
    """数据集文件不符合 schema：出题文件本身有错，评测结论不可信。"""


def dataset_kind(path: Path) -> DatasetKind:
    """数据集种类按文件名判定；themed_* 前缀 = 主题化题集，其余按基线题集。"""
    name = path.name
    if name in _BY_FILENAME:
        return _BY_FILENAME[name]
    return "themed_cases" if name.startswith("themed") else "cases"


def _schema_for(kind: DatasetKind) -> dict[str, Any]:
    """拼出按 kind 取子 schema 的文档：$defs 共享，闭合（unevaluatedProperties）在叶子。"""
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return {"$ref": f"#/$defs/{kind}", "$defs": document["$defs"]}


def validate_rows(rows: Any, kind: DatasetKind) -> None:
    """按 schema 校验数据集行；不过抛 DatasetError（带第一条违规的定位与原因）。"""
    validator = Draft202012Validator(_schema_for(kind))
    error = best_match(validator.iter_errors(rows))
    if error is not None:
        where = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise DatasetError(f"{kind} 数据集不合规 @ {where}: {error.message}")


def dataset_meta(path: Path) -> dict[str, str]:
    """数据集指纹：文件名 + 内容 SHA256（字节口径，跨平台稳定）。"""
    return {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def load_dataset(path: Path) -> tuple[list[dict], dict[str, str]]:
    """读数据集 = 校验 + 指纹；返回 (rows, meta)，meta 随报告落盘。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    validate_rows(raw, dataset_kind(path))
    return raw, dataset_meta(path)
