"""官方页面字段证据（M7，spec 2026-10-08 §13.1）：抓取守卫 + 确定性提取 + 冲突降级。

职责（少量可解释的官方页面 adapter，不是通用爬虫）：
- ``extract_evidence``：纯函数解析一张官方公告页的 HTML，确定性提取字段要点
  （票价免/收、是否实名预约分时）；只对传入的 HTML 负责——页面内容变了证据
  随之变，**绝不硬编码任何景区"永远免费/永远这个开放时间"**；
- ``apply_evidence_conflicts``：官方证据 vs 行程条目当前字段值的冲突判定——
  旧收费话术与新证据冲突时记冲突并把该字段核验状态降级（值撤回为 unknown，
  不自动改成新值：日期适用性/页面时效未经二次核实，宁付一次人工确认）；
- ``fetch_official_page``：经 ExternalClient 的受限抓取——仅 http(s)、拒绝私网/
  回环/链路本地地址、响应上限 2MB、超时 10s、单跳重定向且重定向目标同样禁私网；
  网页文本只作数据不作指令（本模块无任何执行面：提取是正则匹配，不 eval、
  不拼接执行、不跟随页面内的任何"提示"）。

证据边界（spec 原文）：
- 证据记录 URL、发布时间（没有则 unknown=None）、observed_at、字段与摘录位置；
- 没有引用支持的字段不升级 observed/verified——本模块升级只到
  ``partially_verified`` + ``value_kind="observed"``（页面观测一次，无时效担保）；
- "官方没查到"（请求失败/页面解析不出）≠ 免费、不营业、不用预约：字段原样保留
  unknown/估算，绝不因失败而放开任何肯定结论；
- 坐标证据不传染票价：本模块只触碰 ``evidence.fields`` 里出现的字段键，
  ``fact_evidence`` 的其余键（coordinates 等）原样不动。

依赖：app.common（ExternalClient/api_client/contact_user_agent）、app.schemas（FactEvidence
复用——单一真源；只有当前字段无法表达时才扩 schema，本模块零契约扩展：冲突的
"待复核"语义由 verification_status="unverified" + review_requirement="before_departure"
+ 返回的 FieldConflict 明细共同表达， FactEvidence 现有值域足够）。
"""

from __future__ import annotations

import html as _html
import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.common.external_client import ExternalClient
from app.common.http_client import api_client, contact_user_agent
from app.schemas.trip import FactEvidence

#: 票价证据值：free（免票）/ paid:<数字>；预约证据值：required（实名预约分时）。
TICKET_FREE = "free"
RESERVATION_REQUIRED = "required"
#: 冲突降级后字段进入"待复核"：值撤回 + 必须出发前核实（复用 FactEvidence 现有值域）。
NEEDS_REVIEW_STATUS = "unverified"


class OfficialPageError(ValueError):
    """抓取守卫拒绝（非 http(s)、私网地址、超限、多跳重定向）；不冒充为"没查到"。"""


@dataclass(frozen=True)
class OfficialPageEvidence:
    """一张官方页面给出的字段级证据（M7，spec §13.1 最小记录面）。"""

    url: str
    #: 本次观测时间（ISO）；缓存命中时保留首次抓取时刻——它就是证据的观测时点
    observed_at: str
    #: 页面发布时间（ISO）；页面没写就是 None（unknown），不猜
    published_at: str | None
    #: 关键句摘录（提取依据，供人工复核定位）
    excerpt: str
    #: 提取出的字段：{"ticket": "free"|"paid:<n>", "reservation": "required"}（缺省键 = 页面没提）
    fields: dict[str, str]


@dataclass(frozen=True)
class FieldConflict:
    """官方证据与行程条目当前字段值的冲突记录（降级依据，随报告/日志透出）。"""

    field: str
    current_value: object
    evidence_value: str
    evidence_url: str
    observed_at: str
    reason: str


# ---- 确定性提取（正则匹配，不是理解；只认本 adapter 登记的要点句式） ------------

_TICKET_FREE_RE = re.compile(r"(免票|免门票|免费开放|免费参观|不再收取门票)")
_TICKET_PAID_RE = re.compile(r"门票[^。<>]{0,24}?(\d+(?:\.\d+)?)\s*元")
_RESERVATION_RE = re.compile(r"(实名预约|分时游览|分时预约|预约入园|预约参观)")
_PUBLISHED_RE = re.compile(r"发布时间[：:]\s*(\d{4}-\d{1,2}-\d{1,2})")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _page_text(html: str) -> str:
    """HTML → 纯文本（去标签 + 实体解码 + 压空白）；只作匹配输入，无任何执行面。"""
    text = _html.unescape(_TAG_RE.sub("", html))
    return _WS_RE.sub(" ", text)


def _excerpt_of(text: str, match: re.Match[str], *, limit: int = 100) -> str:
    """命中点附近的有界摘录（前后各取一段），供人工复核定位证据出处。"""
    start = max(match.start() - 40, 0)
    end = min(match.end() + 120, len(text))
    return text[start:end].strip()[:limit]


def extract_evidence(html: str, *, fetched_at: str, url: str = "") -> OfficialPageEvidence | None:
    """从一张官方公告页 HTML 确定性提取字段证据；认不出的页面返回 None（unknown）。

    提取不是理解：命中关键词才算证据，未命中 = 本 adapter 表达不了该页面，
    宁可 None 交给上层保持 unknown，也不产出似是而非的字段值。
    """
    text = _page_text(html)
    fields: dict[str, str] = {}
    excerpts: list[str] = []
    free = _TICKET_FREE_RE.search(text)
    if free is not None:
        fields["ticket"] = TICKET_FREE
        excerpts.append(_excerpt_of(text, free))
    else:
        paid = _TICKET_PAID_RE.search(text)
        if paid is not None:
            # 免票表述优先：同页既有"免票"又有历史票价（如飞来峰另计）时不误判收费
            fields["ticket"] = f"paid:{paid.group(1)}"
            excerpts.append(_excerpt_of(text, paid))
    reservation = _RESERVATION_RE.search(text)
    if reservation is not None:
        fields["reservation"] = RESERVATION_REQUIRED
        excerpts.append(_excerpt_of(text, reservation))
    if not fields:
        return None
    published = _PUBLISHED_RE.search(text)
    published_at = published.group(1) if published is not None else None
    return OfficialPageEvidence(
        url=url,
        observed_at=fetched_at,
        published_at=published_at,
        excerpt="；".join(dict.fromkeys(excerpts))[:240],
        fields=fields,
    )


# ---- 抓取守卫（限域 / 禁私网 / 大小 / 超时 / 单跳重定向） -----------------------

_ALLOWED_SCHEMES = ("http", "https")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_FETCH_TIMEOUT_SECONDS = 10.0

#: 官方页抓取通道：TTL 缓存（同一 URL 一次生成内不重复抓）、熔断、字节上限。
OFFICIAL_PAGE_CLIENT: ExternalClient = ExternalClient(
    name="official_pages",
    ttl_seconds=6 * 3600,
    negative_ttl_seconds=300,
    max_response_bytes=_MAX_RESPONSE_BYTES,
    timeout_seconds=_FETCH_TIMEOUT_SECONDS,
    retry_attempts=0,
)


def _host_addresses(host: str) -> list[str]:
    """主机名 → 全部解析地址；字面 IP 直接返回。解析失败按拒绝处理（fail closed）。

    模块级函数（非内联）是给测试的接缝：守卫单测 monkeypatch 它来离线模拟
    "公网域名解析成功"，不触真实 DNS。
    """
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host.strip("[]"), None)
    except OSError:
        return []
    return [str(info[4][0]) for info in infos]


def _is_forbidden_address(address: str) -> bool:
    """私网/回环/链路本地/未指定/组播一律拒绝（127/8、10/8、172.16/12、192.168/16、
    169.254/16、::1 等均被 is_private/is_loopback/is_link_local 覆盖）。"""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True
    return (
        parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified or parsed.is_multicast
    )


def validate_public_http_url(url: str) -> str:
    """守卫：仅 http(s)、有主机名、全部解析地址均非私网；返回归一后的跳转目标。"""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise OfficialPageError(f"官方页面仅允许 http(s) 抓取，收到 scheme：{parsed.scheme or '(空)'}")
    host = parsed.hostname or ""
    if not host:
        raise OfficialPageError("官方页面 URL 缺少主机名")
    addresses = _host_addresses(host)
    if not addresses or any(_is_forbidden_address(address) for address in addresses):
        raise OfficialPageError(f"官方页面抓取拒绝私网/回环/不可解析地址：{host}")
    return url


def _guarded_get(client: ExternalClient, http: httpx.Client, url: str) -> bytes:
    """守卫内 GET：主请求 + 至多一跳重定向（重定向目标同样过全量守卫）。"""
    headers = {"User-Agent": contact_user_agent()}
    validate_public_http_url(url)
    response = http.get(url, headers=headers, timeout=client.timeout_seconds, follow_redirects=False)
    if response.status_code in (301, 302, 303, 307, 308):
        target = urljoin(url, str(response.headers.get("location") or ""))
        validate_public_http_url(target)
        response = http.get(target, headers=headers, timeout=client.timeout_seconds, follow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308):
            raise OfficialPageError("官方页面抓取仅允许单跳重定向")
    response.raise_for_status()
    data = client.clamp_bytes(response.content)
    if data is None:
        raise OfficialPageError(f"官方页面响应超过 {_MAX_RESPONSE_BYTES} 字节上限")
    return data


def fetch_official_page(url: str, *, http_client: httpx.Client | None = None) -> OfficialPageEvidence | None:
    """受守卫抓取一张官方页并提取证据；失败/认不出 → None（unknown，不冒充结论）。

    守卫分两层，按"谁控制"定语义：
    - 第一跳的 scheme/主机在**进入取数通道之前**校验——这是调用方输入，配置错误/
      恶意入参要 fail fast 可闻（OfficialPageError），不能被通道的异常兜底吞成
      "没查到"；
    - 页面控制的中途拒绝（重定向目标私网、多跳、响应超 2MB）在 loader 内发生，
      经 ExternalClient 统一降级为 None（负缓存短窗）——与全部取数调用方的降级
      哲学一致：恶意页面不能炸生成流程，"拒绝"与"没查到"都落 unknown。
    """
    validate_public_http_url(url)
    http = http_client if http_client is not None else api_client()
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")

    def _load() -> OfficialPageEvidence | None:
        raw = _guarded_get(OFFICIAL_PAGE_CLIENT, http, url)
        return extract_evidence(raw.decode("utf-8", errors="replace"), fetched_at=fetched_at, url=url)

    return OFFICIAL_PAGE_CLIENT.call(f"official-page:{url}", _load)


# ---- 冲突判定与降级（结论面：复用 FactEvidence，零契约扩展） ---------------------


def _paid_amount(value: str) -> float | None:
    try:
        return float(value.split(":", 1)[1]) if value.startswith("paid:") else None
    except (IndexError, ValueError):
        return None


def _field_conflicts(field: str, current: Any, evidence_value: str) -> bool:
    """当前值与官方证据是否冲突；当前值缺失（None）= 无主张，采信证据不算冲突。"""
    if current is None:
        return False
    if field == "ticket":
        try:
            current_amount = float(current)
        except (TypeError, ValueError):
            current_amount = None
        amount = _paid_amount(evidence_value)
        if evidence_value == TICKET_FREE:
            return current_amount is not None and current_amount > 0
        # 页面明说收费：金额不一致才是冲突（收费 45 vs 记 80）；原值不是数字则无从比对
        return amount is not None and current_amount is not None and current_amount != amount
    if field == "reservation":
        # 页面要求实名预约分时，条目却记了"无需预约"——正是 spec 点名的误判
        return evidence_value == RESERVATION_REQUIRED and current is False
    return False


def _adopt_evidence(fact: FactEvidence, evidence: OfficialPageEvidence) -> None:
    """有引用支持才升级：observed + partially_verified + 来源/观测时间/出发前复核。

    升级只到 partially_verified：页面是观测一次的快照，没有时效担保，不冒充 verified。
    """
    fact.source_url = evidence.url
    fact.source_ref = evidence.published_at or evidence.observed_at
    fact.provider = "official-page"
    fact.retrieved_at = evidence.observed_at
    fact.verification_status = "partially_verified"
    fact.value_kind = "observed"
    fact.freshness_status = "unknown"
    fact.review_requirement = "before_departure"


def apply_evidence_conflicts(
    current_values: dict[str, Any],
    current_fields: dict[str, FactEvidence],
    evidence: OfficialPageEvidence,
) -> list[FieldConflict]:
    """官方证据 vs 条目当前字段：冲突 → 记录 + 撤回旧值 + 降级待复核；一致/缺失 → 采信升级。

    - 字段键空间与 ``evidence.fields`` 对齐（当前登记：ticket / reservation）——
      坐标等其他 fact_evidence 键不会被本函数触碰（证据不跨字段传染）；
    - 冲突时**不自动改写成新值**：值撤回为 None（unknown），核验状态降
      verification_status="unverified"、review_requirement="before_departure"；
      新值是否采信由二次核实决定——这是"旧收费话术被冲突证据否决"的保守语义；
    - 无当前值或与证据一致：采信证据（observed + 来源 URL/时间 + 摘录），
      票价 free 落 0.0（费用 0 有证据支持）。
    """
    conflicts: list[FieldConflict] = []
    for field, evidence_value in evidence.fields.items():
        fact = current_fields.get(field)
        if fact is None:
            fact = FactEvidence()
            current_fields[field] = fact
        current = current_values.get(field)
        if _field_conflicts(field, current, evidence_value):
            conflicts.append(
                FieldConflict(
                    field=field,
                    current_value=current,
                    evidence_value=evidence_value,
                    evidence_url=evidence.url,
                    observed_at=evidence.observed_at,
                    reason=f"官方页面（{evidence.published_at or '发布时间未知'} 观测于 "
                    f"{evidence.observed_at}）与当前字段值冲突：{current!r} vs {evidence_value}",
                )
            )
            # 撤回旧值 + 降级待复核；证据 URL 只进冲突记录（值已 unknown，不挂引用冒充已核实）
            current_values[field] = None
            fact.verification_status = NEEDS_REVIEW_STATUS
            fact.freshness_status = "unknown"
            fact.review_requirement = "before_departure"
            fact.value_kind = "estimated" if field == "ticket" else fact.value_kind
            continue
        if field == "ticket":
            current_values[field] = 0.0 if evidence_value == TICKET_FREE else _paid_amount(evidence_value)
        elif field == "reservation":
            current_values[field] = evidence_value == RESERVATION_REQUIRED
        _adopt_evidence(fact, evidence)
    return conflicts


def collect_field_evidence(
    current_values: dict[str, Any],
    current_fields: dict[str, FactEvidence],
    url: str,
    *,
    http_client: httpx.Client | None = None,
) -> list[FieldConflict]:
    """抓取→提取→冲突判定的容错编排：任何失败都保持字段原样（unknown 不被误判成
    免费/不营业/不用预约），spec §13.1"官方没查到 ≠ 免费"的落地入口。"""
    try:
        evidence = fetch_official_page(url, http_client=http_client)
    except OfficialPageError:
        return []
    except Exception:
        return []
    if evidence is None:
        return []
    return apply_evidence_conflicts(current_values, current_fields, evidence)
