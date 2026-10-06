"""切流量契约门禁：**前端与活栈测试调用的每个路径，都必须由 FastAPI 后端提供**。

为什么需要这道门禁：M7-b 切流量时前端一行未改就整体打到新后端，于是出现了过去不存在
的失效模式——"前端在调、后端没有"的路径不会让任何离线用例变红，只会在用户点下去的那
一刻 404/500。同理 `tests/api` 是迁移的**切换闸门**（PLAN §2），它引用的路径若已改名或
消失，闸门自己会空转成绿色。

两道扫描都是静态的：解析源码里的路径字面量 → 归一化占位符 → 与装配后的 `main.app.routes`
求差集。零外部依赖、零服务启动，所以它能进 CI 的离线作业。
"""

from __future__ import annotations

import re

from app.common.config import BASE_DIR

REPO_ROOT = BASE_DIR.parent
FRONTEND_SRC = REPO_ROOT / "travel-frontend-react" / "src"
LIVE_TESTS = BASE_DIR / "tests" / "api"

_NORMALIZE_RE = re.compile(r"\{[^}]*\}")

# 前端调用点：axios 封装（相对 baseURL=/api）、下载实例、原生 fetch 与 EventSource（绝对路径）
_CALL_RES = (
    ("GET", re.compile(r"\brequestGet\s*(?:<[^>]*>)?\(\s*")),
    ("POST", re.compile(r"\brequestPost\s*(?:<[^>]*>)?\(\s*")),
    ("PUT", re.compile(r"\brequestPut\s*(?:<[^>]*>)?\(\s*")),
    ("DELETE", re.compile(r"\brequestDelete\s*(?:<[^>]*>)?\(\s*")),
    ("GET", re.compile(r"\bdownloadClient\.get\s*\(\s*")),
    ("GET", re.compile(r"\bEventSource\s*\(\s*")),
)

# React 壳的 fetch 封装（sinan.ts apiRequest）：首参是路径字面量，动词在选项对象里，
# 与裸 fetch 同一套窗口探测。React 重写后其调用点曾整层绕过本契约（fetch 透传被
# 折叠成 /api{}），这里把它纳回扫描——门禁只升不降。
_APIREQUEST_RE = re.compile(r"\bapiRequest\s*(?:<[^>]*>)?\(\s*")

_FETCH_RE = re.compile(r"\bfetch\s*\(\s*")
_FETCH_METHOD_RE = re.compile(r"method\s*:\s*['\"](\w+)['\"]")
# fetch 的第二个实参（选项对象）里可能覆盖动词，窗口取调用后的若干字符即可覆盖本仓写法
_FETCH_OPTIONS_WINDOW = 400

_LIVE_PATH_RE = re.compile(r"""f?["'](/api/[^"']*)["']""")
_BRACE_GROUP_RE = re.compile(r"\{[^{}]*\}", re.S)


def _normalize(path: str) -> str:
    """`{id}` / `{itin_id}` / `${id}` 一律折叠成 `{}`：参数名叫什么不重要，形状才重要。"""
    trimmed = "/" + path.strip().lstrip("/")
    # 查询串不参与路由形状：/itinerary?view=… 就是 /itinerary
    trimmed = trimmed.split("?", 1)[0]
    if len(trimmed) > 1:
        trimmed = trimmed.rstrip("/")
    return _NORMALIZE_RE.sub("{}", trimmed)


def _app_routes() -> dict[str, set[str]]:
    import main

    routes: dict[str, set[str]] = {}
    for route in main.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or not path.startswith("/api"):
            continue
        for method in methods:
            if method not in ("HEAD", "OPTIONS"):
                routes.setdefault(_normalize(path), set()).add(method)
    return routes


def _first_argument(text: str, start: int) -> str:
    """取出调用表达式的第一个实参原文（深度感知，跳过字符串里的括号与逗号）。"""
    depth = 0
    quote: str | None = None
    out: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            out.append(char)
            if char == quote and text[index - 1] != "\\":
                quote = None
        elif char in "\"'`":
            quote = char
            out.append(char)
        elif char in "([{":
            depth += 1
            out.append(char)
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
            out.append(char)
        elif char == "," and depth == 0:
            break
        else:
            out.append(char)
        index += 1
    return "".join(out).strip()


def _call_span(text: str, start: int) -> str:
    """取整个调用表达式的原文（到与之配对的收括号为止）：跨行选项对象全覆盖，
    且不会把下一个调用的 `method:` 吸进本调用的探测窗口。"""
    depth = 0
    quote: str | None = None
    out: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        out.append(char)
        if quote:
            if char == quote and text[index - 1] != "\\":
                quote = None
        elif char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif char in "]}":
            depth -= 1
        index += 1
    return "".join(out)


def _literal_path(expression: str) -> str | None:
    """把「模板串 / 字符串拼接」还原成路径：`${id}`、`+ id +` 都折叠成 `{}`。"""
    expr = re.sub(r"\$\{[^}]*\}", "{}", expression)
    expr = re.sub(r"\+\s*[A-Za-z_$][\w$.]*\s*\+", "+ {} +", expr)
    cleaned = re.sub(r"[\"'`\s+]", "", expr)
    if not cleaned.startswith("/"):
        return None
    return cleaned


def _frontend_call_sites() -> list[tuple[str, str, str]]:
    """返回 [(文件, 方法, 归一化路径)]；只扫 src/api 与 src/composables 下的非测试文件。"""
    sites: list[tuple[str, str, str]] = []
    files = sorted(
        [p for p in (FRONTEND_SRC / "api").glob("*.ts") if not p.name.endswith(".test.ts")]
        + sorted((FRONTEND_SRC / "composables").glob("*.ts"))
    )
    for file in files:
        text = file.read_text(encoding="utf-8")
        for method, pattern in _CALL_RES:
            for match in pattern.finditer(text):
                literal = _literal_path(_first_argument(text, match.end()))
                if literal is None:
                    continue
                path = literal if literal.startswith("/api") else "/api" + literal
                sites.append((file.name, method, _normalize(path)))
        for match in _APIREQUEST_RE.finditer(text):
            literal = _literal_path(_first_argument(text, match.end()))
            if literal is None:
                continue
            window = _call_span(text, match.end())
            declared = _FETCH_METHOD_RE.search(window)
            method = declared.group(1).upper() if declared else "GET"
            path = literal if literal.startswith("/api") else "/api" + literal
            sites.append((file.name, method, _normalize(path)))
        for match in _FETCH_RE.finditer(text):
            literal = _literal_path(_first_argument(text, match.end()))
            if literal is None or not literal.startswith("/api"):
                continue
            window = text[match.end() : match.end() + _FETCH_OPTIONS_WINDOW]
            declared = _FETCH_METHOD_RE.search(window)
            method = declared.group(1).upper() if declared else "GET"
            sites.append((file.name, method, _normalize(literal)))
    return sites


def _live_contract_paths() -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    for file in sorted(LIVE_TESTS.glob("*.py")):
        # 先折叠 {…} 分组：f-string 里的 {detail['id']} 含引号，会让路径正则在中间断掉
        text = _BRACE_GROUP_RE.sub("{}", file.read_text(encoding="utf-8"))
        for raw in _LIVE_PATH_RE.findall(text):
            paths.append((file.name, _normalize(raw)))
    return paths


def test_scans_are_not_silently_empty() -> None:
    """守卫自身的假绿防护：扫描集为空时必须失败（曾有过"扫了个寂寞还全绿"的先例）。"""
    assert len(_frontend_call_sites()) >= 20, "前端调用点扫描为空，门禁形同虚设"
    assert len(_live_contract_paths()) >= 10, "活栈测试路径扫描为空，门禁形同虚设"
    assert len(_app_routes()) >= 40, "后端路由表过小，装配可能没跑起来"


def test_frontend_call_sites_are_all_served() -> None:
    routes = _app_routes()
    missing: list[str] = []
    method_mismatch: list[str] = []
    for file, method, path in _frontend_call_sites():
        available = routes.get(path)
        if available is None:
            missing.append(f"{file}: {method} {path}")
        elif method not in available:
            method_mismatch.append(f"{file}: {method} {path}（后端只有 {sorted(available)}）")
    assert not missing, "前端在调、后端没有的路径（切流量后必 404）: " + "; ".join(missing)
    assert not method_mismatch, "前端方法与后端不一致: " + "; ".join(method_mismatch)


def test_live_contract_tests_only_call_existing_paths() -> None:
    """tests/api 是迁移的切换闸门：它引用的路径必须都在（否则闸门空转）。"""
    routes = _app_routes()
    missing = [f"{file}: {path}" for file, path in _live_contract_paths() if path not in routes]
    assert not missing, "活栈契约测试引用了不存在的路径: " + "; ".join(missing)


def test_frontend_has_no_hardcoded_java_host() -> None:
    """切流量后前端只能走相对 /api：写死 :8080 会让它悄悄回到旧后端。"""
    offenders: list[str] = []
    for file in sorted(FRONTEND_SRC.rglob("*")):
        if file.suffix != ".ts" or file.name.endswith(".test.ts"):
            continue
        for line_no, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
            if ":8080" in line:
                offenders.append(f"{file.relative_to(FRONTEND_SRC)}:{line_no}")
    assert not offenders, "前端出现指向旧 Java 网关的硬编码地址: " + "; ".join(offenders)
