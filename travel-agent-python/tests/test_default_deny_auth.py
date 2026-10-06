"""默认拒绝门禁：`/api` 下每个路由都必须**显式**带鉴权依赖，匿名可达面只准收窄。

为什么需要这道门禁：`app/api/security.py` 的 `enforce_business_auth` 挂在**每条路由**
上（只有一个 router 例外），所以"新增端点时忘记加依赖"的失效模式是静默的——路由正常
工作、离线用例全绿、只有真实访客能看见它匿名。`test_cutover_contract.py` 只比对路径
存在性，看不见这件事。

这里把方向反过来：从装配后的 `main.app.routes` 枚举出真实的依赖闭包，凡是不带业务
闸门（或 agent 面的内部令牌）的路由，必须出现在下面的**具名豁免清单**里。清单外的新
路由即红；清单只准变短（INV-1）。

同时钉住第二件事：`PUBLIC_PATHS` 是**前缀**匹配，`/api/uploads/` 下再加一条 POST 就会
匿名可写。所以非 GET 且匿名可达的路由也被逐条钉住，不靠"前缀看起来无害"。
"""

from __future__ import annotations

from typing import Any

from fastapi.routing import APIRoute

_BUSINESS_GUARDS = frozenset({"enforce_business_auth", "require_user", "require_admin"})
_AGENT_GUARD = "require_internal_token"
_AGENT_PREFIX = "/api/agent/"

# 完全没有鉴权依赖的业务路由（只准变短）。/api/auth/* 的登录与注册必须无票可达，
# 否则没人能进来；暴力尝试由 business/auth.py 的按 IP 滑动窗口挡住。
_UNGUARDED_BUSINESS = frozenset(
    {
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/register"),
    }
)

# 匿名可达的**写**路由（只准变短）。比上面那份宽：`logout-all` 带着
# `Depends(enforce_business_auth)`（它要知道是谁），但 `/api/auth/` 落在 PUBLIC_PATHS
# 前缀里，所以不带票的请求同样能到达它——它自己用 ApiError(401) 挡住。
# 这一份钉的是"前缀放行有没有把一条写端点变成匿名可写"。
# 例外登记（2026-09-30，前端错误探针）：POST /api/client-errors 刻意匿名可写——
# 登录页自身的崩溃恰恰发生在拿不到会话时。滥用面有界：只写结构化日志不落库、
# 全字段上限、按 IP 分钟窗 30 次超限 429（见 business/client_errors.py）。
# 复核条件：日志面出现灌水即收回匿名性（改走强制会话），见 AGENTS.md 哲学 #2 注记。
_ANONYMOUS_WRITES = _UNGUARDED_BUSINESS | frozenset({("POST", "/api/auth/logout-all"), ("POST", "/api/client-errors")})

# agent 面走 X-Agent-Token 内部令牌，不是用户会话；两个探活端点必须匿名，
# 否则 start-all.ps1 与 Dockerfile HEALTHCHECK 在拿到令牌前就判死。
_ANONYMOUS_AGENT = frozenset(
    {
        ("GET", "/api/agent/health"),
        ("GET", "/api/agent/hello"),
    }
)

_SAFE_METHODS = frozenset({"HEAD", "OPTIONS"})


def _dependency_names(node: Any) -> set[str]:
    """递归展开一个 dependant/Depends 树，取出所有 callable 的名字。"""
    names: set[str] = set()
    stack: list[Any] = [node]
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        call = getattr(cur, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", str(call)))
        stack.extend(getattr(cur, "dependencies", None) or [])
    return names


def _guarded_routes() -> list[tuple[str, str, set[str]]]:
    """返回 (method, path, 依赖名集合)，覆盖装配后的全部 `/api` 端点。"""
    import main

    rows: list[tuple[str, str, set[str]]] = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if not path.startswith("/api") or any(frag in path for frag in ("/docs", "/openapi", "/redoc")):
            continue
        names = _dependency_names(route.dependant)
        for dep in route.dependencies:
            names |= _dependency_names(dep)
        for method in sorted(set(route.methods) - _SAFE_METHODS):
            rows.append((method, path, names))
    return rows


def test_scan_is_not_silently_empty() -> None:
    """装配没跑起来时本文件会全绿——先把「扫到了东西」钉住（同 cutover 门禁手法）。"""
    rows = _guarded_routes()
    assert len(rows) >= 100, f"只扫到 {len(rows)} 条 /api 端点，应用装配可能失败"


def test_business_routes_all_carry_a_guard() -> None:
    """非 agent 面：每条路由的依赖闭包里必须有业务闸门，否则必须在豁免清单上。"""
    unguarded = {
        (method, path)
        for method, path, names in _guarded_routes()
        if not path.startswith(_AGENT_PREFIX) and not (names & _BUSINESS_GUARDS)
    }
    assert not (unguarded - _UNGUARDED_BUSINESS), (
        f"这些端点没有鉴权依赖，等于匿名暴露：{sorted(unguarded - _UNGUARDED_BUSINESS)}。"
        "加 Depends(enforce_business_auth)；确需匿名请同时把判据写进本文件的豁免清单。"
    )
    assert not (_UNGUARDED_BUSINESS - unguarded), (
        f"豁免清单里有已经不再匿名的端点：{sorted(_UNGUARDED_BUSINESS - unguarded)}，请删掉。"
    )


def test_agent_routes_all_carry_the_internal_token() -> None:
    """/api/agent/* 生成类端点每次调用消耗真实 LLM 配额，必须过内部令牌。"""
    unguarded = {
        (method, path)
        for method, path, names in _guarded_routes()
        if path.startswith(_AGENT_PREFIX) and _AGENT_GUARD not in names
    }
    assert not (unguarded - _ANONYMOUS_AGENT), (
        f"这些 agent 端点没有内部令牌校验：{sorted(unguarded - _ANONYMOUS_AGENT)}"
    )
    assert not (_ANONYMOUS_AGENT - unguarded), f"令牌豁免清单过期：{sorted(_ANONYMOUS_AGENT - unguarded)}，请删掉。"


def test_no_anonymous_writes_under_public_prefixes() -> None:
    """`PUBLIC_PATHS` 按前缀放行：`/api/uploads/` 下多出一条 POST 即匿名可写。

    这道断言与上面两条独立——上面看"有没有依赖"，这里看"依赖是否真的挡住了写"
    （带 `enforce_business_auth` 但落在公开前缀里的路由，匿名请求依然放行）。
    """
    from app.api.security import is_public_path

    anonymous_writes = {
        (method, path) for method, path, _names in _guarded_routes() if method not in ("GET",) and is_public_path(path)
    }
    assert anonymous_writes <= _ANONYMOUS_WRITES, (
        f"这些写端点因 PUBLIC_PATHS 前缀匹配而匿名可达：{sorted(anonymous_writes - _ANONYMOUS_WRITES)}。"
        "把路径挪出公开前缀，或收窄 app/api/security.py 里的前缀。"
    )
