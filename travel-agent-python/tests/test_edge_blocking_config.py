"""边缘拦截门禁：agent 内部面与 `/mcp` 必须在 nginx 与端口发布两层都被挡住。

为什么需要这道门禁：`app/api/agent.py:require_internal_token` 与 `app/api/mcp.py` 的
安全论证都写明了"真正把它带到公网的是边缘——nginx 必须以 `deny all` 丢掉
`/api/agent/`"，而 `docker-compose.yml` 把宿主机 8000 只绑回环的理由同样只写在注释里。
也就是说，**当前唯一的执行者是注释**。改一次 nginx.conf（挪 location、复制粘贴模板、
换 ingress）不会让任何 Python 用例变红，症状却是在外网直接可调 `/api/agent/v1/generate`
与带 `user_id` 的 MCP 写工具。

这里把两份配置文件当作被测代码读，锁三件事：
1. `/api/agent/` 与 `/mcp` 两个 location 里存在 `deny all`；
2. `/api/` 的 XFF 是**替换**而非追加（追加时链首永远是客户端可伪造的那一段，
   `app/common/client_ip.py` 的从右往左归因随即失效，按 IP 的登录限速形同虚设）；
3. 没有任何宿主机端口把后端 8000 绑到非回环地址。

只读文本、不依赖 yaml 库（INV-10：不为一条门禁新增依赖）。
"""

from __future__ import annotations

import re

from app.common.config import BASE_DIR

REPO_ROOT = BASE_DIR.parent
NGINX_CONF = REPO_ROOT / "travel-frontend-react" / "nginx.conf"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

# 内部面清单：这些 location 必须匿名不可达（凭 X-Agent-Token 直连内网端口）
MUST_DENY: tuple[str, ...] = ("/api/agent/", "/mcp")

BACKEND_HOST_PORT = "8000"


def _nginx_text() -> str:
    assert NGINX_CONF.is_file(), f"缺少 {NGINX_CONF}：compose 的前端服务以它为准，文件没了说明布局变了"
    return NGINX_CONF.read_text(encoding="utf-8")


def _location_bodies(text: str) -> dict[str, str]:
    """把 `location <前缀> { ... }` 配平花括号切成 {前缀: 指令文本}。

    注释行在切之前就去掉：解释"为什么必须替换而不是追加"的那段注释里同时出现了两种
    写法，留着它，下面的指令检查会把文档当成代码。
    """
    stripped = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    bodies: dict[str, str] = {}
    for match in re.finditer(r"location\s+(=?\s*[^{]+?)\s*\{", stripped):
        prefix = match.group(1).strip()
        depth = 1
        pos = match.end()
        while pos < len(stripped) and depth:
            if stripped[pos] == "{":
                depth += 1
            elif stripped[pos] == "}":
                depth -= 1
            pos += 1
        bodies[prefix] = stripped[match.end() : pos - 1]
    return bodies


def test_internal_planes_are_denied_at_the_edge() -> None:
    bodies = _location_bodies(_nginx_text())
    for prefix in MUST_DENY:
        assert prefix in bodies, f"nginx.conf 里已不存在 `location {prefix}` —— 内部面失去显式拦截"
        assert "deny all" in bodies[prefix], f"`location {prefix}` 必须 `deny all`，该面只准内网直连"


def test_proxy_replaces_xff_instead_of_appending() -> None:
    """追加式 XFF 会让最左跳由客户端自控，按 IP 的限速/锁桶全部失效。"""
    bodies = _location_bodies(_nginx_text())
    api = bodies.get("/api/", "")
    assert "$remote_addr" in api, "/api/ 的 X-Forwarded-For 必须由 nginx 以 $remote_addr 赋值"
    assert "$proxy_add_x_forwarded_for" not in api, (
        "/api/ 用了 $proxy_add_x_forwarded_for（追加）：客户端自带的 XFF 会留在链首，"
        "app/common/client_ip.py 从右往左取到的第一个不可信跳即攻击者自选"
    )


def _port_offenders(text: str) -> list[str]:
    """返回把后端服务端口发布到非回环地址的行（`文件:行号: 原文`）。"""
    offenders: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("- "):
            # YAML 序列项：先去掉列表标记，再脱引号，否则 host 会带上 `- "`
            stripped = stripped[2:].strip().strip("\"'")
        if not stripped or stripped.startswith("#"):
            continue
        # 两种写法都会把服务端口发布出去：`HOST:HP:SP` 与裸 `HP:SP`。
        # 后者等价于绑 0.0.0.0，所以它必须是**违规**而不是"匹配不到所以放过"。
        mapping = re.match(r"^(?:(?P<host>[^:]+):)?(?P<host_port>\d+):(?P<service_port>\d+)(?:/.*)?$", stripped)
        if not mapping or mapping.group("service_port") != BACKEND_HOST_PORT:
            continue
        if mapping.group("host") not in ("127.0.0.1", "localhost", "::1"):
            offenders.append(f"{COMPOSE_FILE.name}:{line_no}: {stripped}")
    return offenders


def test_backend_port_is_never_published_off_loopback() -> None:
    """`0.0.0.0:8000` 或裸 `8000:8000` 都让内部面绕过 nginx。"""
    assert COMPOSE_FILE.is_file(), f"缺少 {COMPOSE_FILE}"
    offenders = _port_offenders(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert not offenders, (
        f"后端 8000 被发布到非回环地址（内部面可绕过 nginx 直达）：{offenders}。"
        "改为 `127.0.0.1:8000:8000`，对外只经 nginx 的 /api/ 反代"
    )


def test_compose_still_trusts_the_container_network_for_xff() -> None:
    """compose 若不信任 nginx 所在网段，所有访客会共用一个限速桶。

    `client_ip.py` 只在**直接对端**命中 TRUSTED_PROXIES 时才采信 XFF。容器网络里
    nginx 的地址是动态的，所以默认值必须带网段；只留回环地址是静默失效（症状是
    "所有人都被同一条 429 拖住"，从症状倒不回配置）。
    """
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    match = re.search(r"^\s*TRUSTED_PROXIES:\s*(.+?)\s*$", text, re.M)
    assert match, "docker-compose.yml 不再设置 TRUSTED_PROXIES：nginx 这个跳会变成不可信"
    value = match.group(1)
    assert "/" in value, f"TRUSTED_PROXIES 默认值 {value!r} 没有 CIDR 网段，动态的 nginx 容器地址将不被信任"
