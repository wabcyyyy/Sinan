"""模型角色注册表：把"哪次调用走哪个网关的哪个模型"收敛到唯一解析点。

为什么要有它（2026-10-04）：此前每个用途各占一组平铺 env 键（`LLM_MODEL` /
`LLM_FAST_MODEL` / `LLM_VISION_MODEL` / `JUDGE_LLM_*`），`settings.llm_fast_model`
被 7 个模块 15 处各自读取，能力判断散落在调用点。主通道从百炼换到 DeepSeek 官方后，
三处按通道能力生效的默认值**静默失效**：联网搜索拿模型记忆冒充检索结果（来源还标
web.search）、qwen-vl-plus 与 qwen-max 打到根本不提供它们的网关。加一个角色越来越贵，
失效越来越隐蔽——这就是要收敛的原因。

口径：
- **provider** = 一个网关（base_url + api_key + 能力）。名册 `default`（由
  `LLM_BASE_URL`/`LLM_API_KEY` 定义）/ `dashscope` / `mimo`；名册外的自建网关用 default。
- **role** = 一次用途（`main` / `fast` / `vision` / `judge` / `search` / `stt`），
  `LLM_ROLE_<ROLE>` 的值是 `provider:model`；**裸模型名 = 走 default provider**，
  **留空 = 复用 main**（除 main 自己）。
- **能力**由 provider 决定，本表只承载有消费方的三项：联网搜索（`search`，只有百炼兼容层
  有 `enable_search`）、语音（`audio`，目前只有 MiMo）、json_schema 强约束档
  （`json_schema`，DeepSeek 官方 2026-10-05 起对该档 400，见 `_capabilities`），
  调用点问 `binding(role).capabilities()`。视觉不经本表——闸在 `llm_client.require_model_served`
  按模型名判，DeepSeek 官方 2026-08 起 deepseek-flash 直接收图，主模型即看图。
- **BYOK 优先**：用户自带网关（`llm_route` 的 ContextVar）生效时，它就是这个用户所有
  角色的通道；下面的角色键是**部署默认值**，不盖用户自己的选择。
- **main 备选链**（审计 §3.1.1 / P0-2）：`LLM_MAIN_FALLBACK_PROVIDER`（provider:model，
  留空 = 无备选）定义 main 角色的备选落点；主通道熔断开窗时**解析切到它**——切换
  决策在 llm_client（熔断状态在 llm 侧的 llm_breaker，本模块不反向 import），本模块
  只产出备选数据并在 `validate()` 校验它配得对。
- 配置错误（未知 provider、缺模型名、provider 没配 base_url）由 `validate()` 在启动期
  一次性报出，不留到运行中的第一次调用。

依赖：app.common.config（settings）。本模块只产出**数据**，不构造 LLMClient——
那在 app/common/llm_client.py，避免 llm_client ↔ registry 互相 import。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.common.config import LOCAL_HOSTS, settings
from app.common.llm_route import current_route

logger = logging.getLogger(__name__)

#: 一次调用的用途。新增角色 = 这里加一项 + config.py 加一个 llm_role_<name> 键。
ROLES: tuple[str, ...] = ("main", "fast", "vision", "judge", "search", "stt")

#: 具名 provider 名册；default 由 LLM_BASE_URL / LLM_API_KEY 定义。
PROVIDER_NAMES: tuple[str, ...] = ("default", "dashscope", "mimo")


@dataclass(frozen=True)
class Provider:
    """一个网关：地址、凭据与它**声明会做**的事。"""

    name: str
    base_url: str
    api_key: str
    capabilities: frozenset[str]

    @property
    def available(self) -> bool:
        """该 provider 有没有可对话的地址；有没有 key 由 `configured()` 判。

        注意这不等于"用户配过"：mimo 自带官方默认 base_url，天然 available；
        dashscope 才是留空 = 不可用（见 config.py 具名 provider 段注释）。
        """
        return bool(self.base_url.strip())


@dataclass(frozen=True)
class RoleBinding:
    """一次用途的最终落点（provider + 模型名 + 由 provider 带出的能力）。"""

    role: str
    provider: Provider
    model: str

    @property
    def base_url(self) -> str:
        return self.provider.base_url

    def capabilities(self) -> frozenset[str]:
        return self.provider.capabilities


def _capabilities(name: str, base_url: str) -> frozenset[str]:
    """provider 会什么。search 只在百炼兼容层，audio 目前只在 MiMo（视觉不经本表，见模块 docstring）。

    `json_schema`（response_format 强约束档）：DeepSeek 官方 2026-10-05 实测对该档
    返回 400（同 key 同模型 json_object 200，夜审 R2-F1），上游较 PR-5 spike
    （2026-09-24，当时主通道尚为 DashScope）已变更——唯一 DeepSeek 官方网关不带此位，
    生成出口与 judge 据此降级 json_object；其余网关维持支持口径（与降级前行为一致，
    不为未探测的网关猜能力）。URL 判据与 llm_client._is_deepseek_url 同串：registry
    不反向依赖 llm_client（import 方向见模块 docstring），子串嗅探本表已有 dashscope 先例。
    """
    caps = {"chat"}
    low = (base_url or "").lower()
    if "api.deepseek.com" not in low:
        caps |= {"json_schema"}
    if name == "dashscope" or "dashscope" in low:
        caps |= {"search"}
    if name == "mimo":
        caps |= {"audio"}
    return frozenset(caps)


def provider(name: str) -> Provider:
    """按名取 provider；名册外一律报错（自建网关请用 default）。"""
    if name == "default":
        base, key = settings.llm_base_url, settings.llm_api_key
    elif name == "dashscope":
        base, key = settings.llm_provider_dashscope_base_url, settings.llm_provider_dashscope_api_key
    elif name == "mimo":
        base, key = settings.llm_provider_mimo_base_url, settings.llm_provider_mimo_api_key
    else:
        raise ValueError(f"未知 provider {name!r}：名册是 {list(PROVIDER_NAMES)}（自建网关用 default）")
    return Provider(name, base, key, _capabilities(name, base))


def _parse(role: str, raw: str) -> RoleBinding:
    """`provider:model` / 裸模型名 → RoleBinding；缺模型名当场报错。"""
    name, sep, model = raw.partition(":")
    if not sep:
        name, model = "default", raw
    model = model.strip()
    if not model:
        raise ValueError(f"LLM_ROLE_{role.upper()} 缺模型名：写成 provider:model 或裸模型名")
    return RoleBinding(role, provider(name.strip()), model)


def binding(role: str) -> RoleBinding:
    """该角色的最终落点。

    优先级：**BYOK 路由 > 角色键 > 默认 provider**。用户自带网关（BYOK）生效时，
    它就是这个用户所有 LLM 调用的通道——各角色键是部署默认值，不该盖掉用户自己的选择。
    角色键留空则复用 main（main 自己留空 = default provider + LLM_MODEL）。
    """
    if role not in ROLES:
        raise ValueError(f"未知角色 {role!r}：可用角色 {list(ROLES)}")
    route = current_route()
    if route is not None:
        caps = _capabilities("byok", route.base_url)
        return RoleBinding(role, Provider("byok", route.base_url, route.api_key, caps), route.model)
    raw = {
        "main": settings.llm_role_main,
        "fast": settings.llm_role_fast,
        "vision": settings.llm_role_vision,
        "judge": settings.llm_role_judge,
        "search": settings.llm_role_search,
        "stt": settings.llm_role_stt,
    }[role].strip()
    if not raw:
        if role == "main":
            return RoleBinding("main", provider("default"), settings.llm_model)
        main = binding("main")
        return RoleBinding(role, main.provider, main.model)
    return _parse(role, raw)


def fallback_binding() -> RoleBinding | None:
    """main 角色的备选通道（审计 §3.1.1 / P0-2）：`LLM_MAIN_FALLBACK_PROVIDER`。

    值与角色键同构：`provider:model`（裸模型名 = default provider）；留空 = None
    （默认，无备选，main 解析行为与现在完全一致）。只产出数据不做切换：主通道
    熔断开窗时由 llm_client 的解析点（`_resolve_main_binding`）切到这里。备选也
    必须指向配了 base_url 的 provider，`validate()` 启动期一并校验。
    """
    raw = settings.llm_main_fallback_provider.strip()
    if not raw:
        return None
    name, sep, model = raw.partition(":")
    if not sep:
        name, model = "default", raw
    model = model.strip()
    if not model:
        raise ValueError("LLM_MAIN_FALLBACK_PROVIDER 缺模型名：写成 provider:model 或裸模型名")
    return RoleBinding("main", provider(name.strip()), model)


def model_for(role: str) -> str:
    """该角色的模型名（调用点替掉过去的 settings.llm_fast_model / llm_vision_model）。"""
    return binding(role).model


def json_response_format(role: str, name: str, schema: dict) -> dict:
    """该角色的结构化输出档（夜审 R2-F1）：能力位→请求档位的唯一换算点。

    - 有 `json_schema` 位：PR-5 强约束档（网关保证输出符合 schema，spike 2026-09-24
      在 DashScope 兼容层实证执行）；
    - 无该位（DeepSeek 官方 2026-10-05 实测对 json_schema 400，同 key json_object
      200）：降级 json_object——结构约束退到 system prompt 内嵌的完整 JSON 形状
      （open_*_system_prompt 一直内嵌）+ 坏 JSON 单次修复重试，不再整链 100% 失败。

    生成三出口与 judge 共用；调用点不得自行嗅探 URL。
    """
    if "json_schema" in binding(role).capabilities():
        return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
    return {"type": "json_object"}


def configured(role: str = "main") -> bool:
    """该角色有没有可用的凭据（api_key 非空）——替掉散落的 `settings.llm_api_key` 哨兵。"""
    return bool(binding(role).provider.api_key.strip())


def validate() -> None:
    """启动期校验：每个角色都要解析得出、且指向配了 base_url 的 provider。

    坏配置当场炸（含未知 provider、缺模型名、`LLM_ROLE_X=dashscope:qwen-max` 但
    dashscope 没配地址），而不是等到运行中第一次调用才以 404/超时现身。
    """
    for role in ROLES:
        bound = binding(role)
        if not bound.provider.available:
            raise ValueError(
                f"角色 {role} 指向 provider {bound.provider.name}，但它没配 base_url"
                f"（LLM_PROVIDER_{bound.provider.name.upper()}_BASE_URL）"
            )
    # main 备选链（P0-2）：配了就要解析得出且指向配了 base_url 的 provider——
    # 备选是故障时刻才生效的通道，配错平时完全无症状，必须启动期拦。
    fallback = fallback_binding()
    if fallback is not None and not fallback.provider.available:
        raise ValueError(
            f"LLM_MAIN_FALLBACK_PROVIDER 指向 provider {fallback.provider.name}，"
            f"但它没配 base_url（LLM_PROVIDER_{fallback.provider.name.upper()}_BASE_URL）"
        )
    # 审计 §3.1.6：绑定非回环但 main 角色没配 key——进程照常启动、探活通过，第一个
    # 用户点生成才发现全量失败（与 §3.1.1 的 2026-10-05 事故同款「上线后才发现」
    # 模式，根因在配置）。不是 fail：本地回环开发允许不配 key，这里只让部署日志
    # 显式可见。落在 validate() 而不是 Settings.validate_boot 是 import 方向所致：
    # registry 能看 settings，config 不能反向 import registry；main.py lifespan
    # 对两者在同一启动序列里都有调用。
    if settings.agent_host not in LOCAL_HOSTS and not configured("main"):
        logger.warning(
            "AGENT_HOST=%s 绑定非回环但 main 角色未配置 api_key（LLM_API_KEY 为空）："
            "进程将健康启动、探活通过，但所有生成请求都会失败；"
            "请在 .env 配置 LLM_API_KEY，或确认这是有意的不生成部署",
            settings.agent_host,
        )
