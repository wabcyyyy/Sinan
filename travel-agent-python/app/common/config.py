"""全局运行配置（pydantic-settings 字段定义式，G-1.5）。

职责：
- 从环境变量（.env）读取服务运行所需的全部配置项，集中暴露为单例 settings；
- 启动期配置校验（Settings.validate_boot）：坏配置在 boot 期报清晰错误退出。

实现要点：
- 加载顺序保持既有语义：先 load_dotenv 仓库根 .env、再本模块 .env（load_dotenv 默认
  不覆盖已存在变量，故真实环境变量 > 根 .env > 包 .env），BaseSettings 只读 os.environ；
- 键名与语义稳定：字段名小写与环境变量大小写不敏感对应（agent_host <-> AGENT_HOST）；
  唯一例外 jwt_revocation_prefer_redis 用 validation_alias 映射 APP_JWT_REVOKE_PREFER_REDIS；
- default_factory 默认值（路径/派生 URL）在 factory 内读 env 仍属本文件（INV-7）；
- 实例可变（非 frozen）：测试 monkeypatch.setattr(settings, ...) 兼容；
- validate_boot 由 main.py lifespan 调用，坏配置 RuntimeError -> 进程退出码非 0；
  不放在 import 期，保证测试与脚本 import 不受真实密钥约束。

依赖：python-dotenv、pydantic-settings；内部仅 app.common.config_rules
（数值范围规则表，本文件拆出的纯数据面）。
"""

import base64
import binascii
import ipaddress
import logging
import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.common.config_rules import NUMERIC_RULES, numeric_ok

BASE_DIR = Path(__file__).resolve().parent.parent.parent
# 先加载仓库根目录共享 .env，再加载本模块 .env（同名变量后者优先）
load_dotenv(BASE_DIR.parent / ".env")
load_dotenv(BASE_DIR / ".env")

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})

# 已知占位符前缀（小写匹配，审计 §3.4.2 / P0-5 应用侧）：.env.example 是公开仓库里的
# 文件，其中的示例串人尽皆知——JWT_SECRET 的 replace-with-…-at-least-32-chars 恰好
# 51 字符，能穿过下文 ≥32 的长度校验直上生产（误配 = 任何人可离线伪造含 admin 的
# 会话票）。deploy.sh 在部署侧拦一道，validate_boot 在应用侧再拦一道（两层口径见
# deploy/deploy.sh 的 reject_secret）。真实随机串（openssl rand -hex 32）不会命中
# 任何前缀；空串不在此判（走各自的既有校验）。
_SECRET_PLACEHOLDER_PREFIXES: tuple[str, ...] = ("replace-with", "your-", "changeme")


def _secret_placeholder_mark(value: str) -> str | None:
    """返回命中的占位符前缀原样（如 ``your-``）；非占位串返回 None。"""
    text = value.strip().lower()
    for prefix in _SECRET_PLACEHOLDER_PREFIXES:
        if text.startswith(prefix):
            return prefix
    return None


def parse_trusted_proxy(item: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """把 `TRUSTED_PROXIES` 的一项解析成网段：裸地址按单主机（/32 或 /128）。

    同一解析器供启动期校验与请求期归因（app.common.client_ip）共用——各写一份，
    "配置里写错的代理"会在一边报错、另一边静默不信任。
    """
    text = item.strip()
    if "/" not in text:
        text = f"{text}/32" if ":" not in text else f"{text}/128"
    return ipaddress.ip_network(text)


def _proxy_ok(item: str) -> bool:
    try:
        parse_trusted_proxy(item)
    except ValueError:
        return False
    return True


logger = logging.getLogger(__name__)


def _get(name: str, default: str) -> str:
    """仅供本模块 default_factory 派生默认值使用（INV-7 边界内的环境读取）。"""
    return os.getenv(name, default)


def _default_font_path() -> str:
    """印刷字体（simhei）已随「Java 退役」git mv 进本仓，全仓唯一一份。"""
    return str(BASE_DIR / "app" / "resources" / "fonts" / "simhei.ttf")


def byok_key_material(raw: str) -> bytes:
    """BYOK_ENC_KEY -> 原始 key 字节（宽容缺失 padding）；坏串抛 ValueError。"""
    padded = raw + "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("BYOK_ENC_KEY 不是合法的 urlsafe base64 串") from exc


def _default_redis_url() -> str:
    """显式 REDIS_URL 优先，否则由 REDIS_HOST/REDIS_PORT/REDIS_PASSWORD 派生。

    口令走 URL 而不是 redis-py 参数：`redis_client` 只认 `from_url(...)` 一条路，
    两处各配一份迟早会漂。空口令时派生结果与历史完全一致（不多一个 `@`）。
    """
    host = _get("REDIS_HOST", "localhost")
    port = _get("REDIS_PORT", "6380")
    password = _get("REDIS_PASSWORD", "")
    auth = f":{quote_plus(password)}@" if password else ""
    return f"redis://{auth}{host}:{port}/0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # ---- 服务面 ----
    agent_host: str = "127.0.0.1"
    agent_reload: bool = False
    agent_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    agent_internal_token: str = ""
    agent_deadline_seconds: float = 90
    trusted_proxies: str = "127.0.0.1,0:0:0:0:0:0:0:1,::1"

    # ---- LLM ----
    # 默认 provider（名册与解析口径见 app/common/model_registry.py）
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    # 模型角色：provider:model；裸模型名 = 默认 provider；留空 = 复用 main
    # （fast=内容生成/意图，vision=拍照识图，judge=LLM-as-judge，search=联网补池，stt=语音识别）
    llm_role_main: str = ""
    llm_role_fast: str = ""
    llm_role_vision: str = ""
    llm_role_judge: str = ""
    llm_role_search: str = ""
    llm_role_stt: str = ""
    # 具名 provider（dashscope base_url 留空 = 不可用；mimo 已带官方默认值；名册外的网关请用默认 provider）
    llm_provider_dashscope_base_url: str = ""
    llm_provider_dashscope_api_key: str = ""
    llm_provider_mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    llm_provider_mimo_api_key: str = ""
    llm_timeout: float = 240
    llm_pool_max_connections: int = 20
    llm_pool_max_keepalive: int = 10
    llm_connect_timeout: float = 5
    llm_generation_web_search: bool = False
    # 思考开关（百炼 enable_thinking / DeepSeek thinking）与思考强度（low/high/max）
    llm_enable_thinking: bool | None = True
    llm_reasoning_effort: str | None = "low"

    # ---- 实时信息与联网搜索 ----
    # 实时酒店价默认开启核实（额外触发联网搜索/模型调用）；追求首版速度可设 false。
    live_price_search: bool = True
    max_live_queries: int = 3
    # 餐饮实时价（百炼联网）：与酒店实时价独立开关，同样受 max_live_queries 约束
    live_food_price_search: bool = True
    max_live_food_queries: int = 4
    # 研究/备选池证据不足时用联网搜索补候选（百炼 enable_search；通道无插件时按「未联网」降级）
    web_search_enabled: bool = True

    # ---- 预算与生成后处理 ----
    # 生成后按估算总价是否触发超支修复（reflect 反馈重排）
    budget_hard_constraint: bool = True
    # 超过预算多少比例才触发修复（避免贴近预算反复重试）
    budget_overage_ratio: float = 0.08
    # 餐饮单价相对城市人均餐价的钳制倍数（硬顶/软顶）
    meal_price_hard_cap_ratio: float = 8
    meal_price_soft_cap_ratio: float = 4

    # ---- 路线 ----
    # 路线矩阵校验：本地坐标估算供排程校验与打分，默认关闭
    route_service_enabled: bool = False
    route_mode: str = "walking"
    route_cache_ttl: float = 900
    route_peak_factor: float = 1.25
    route_max_calls: int = 64

    # ---- Agent 运行预算 ----
    tool_max_calls: int = 32
    max_llm_calls: int = 32
    max_token_budget: int = 80000
    # 按用户的闸门（R1-9）：上面两份预算是"按次"的，单用户循环调用拿到的就是
    # N 份按次额度 → 无上限；这两项才是按 principal 的顶。
    user_llm_runs_per_minute: int = 6
    user_daily_llm_runs: int = 100
    # P1-8 同族两项：实时价两端点直烧 SerpApi 共享月池（按人闸门）；agent 直调面
    # 按 token 的进程内分钟窗（token 泄露止损，内网信任面 + 单进程前提）
    user_live_quotes_per_minute: int = 6
    agent_rate_limit_per_minute: int = 30
    # 单次 run 内检索/证据类调用上限（研究补查、联网搜索、外部点位 API）
    max_retrievals: int = 48
    # 研究阶段自己的额度（PLAN-A1 后续）：max_retrievals 是全 run 共用的，三域研究的
    # 补池循环没有"给生成留一口"的概念——实测 1 天 case 研究就烧满检索道，生成一次
    # LLM 都没轮到（产出 0 项草案）；再抬总额会先撞 deadline 实测墙。0 = 不限。
    research_call_limit: int = 12
    max_replans: int = 3
    no_progress_limit: int = 2
    # 并行 worker 数（PR-11 配置化：原 supervisor/_search_pois 硬编码 3）。两者口径同源——
    # 研究三域线程池与 POI 三路检索各自的上限；调大不会更快：外呼节奏由 ExternalClient 车道节流钳制。
    research_workers: int = 3
    poi_search_workers: int = 3

    # ---- 轨迹与用量 ----
    trace_storage_enabled: bool = True
    trace_storage_path: str = Field(default_factory=lambda: str(BASE_DIR / "data" / "agent_traces.jsonl"))
    usage_db_path: str = Field(default_factory=lambda: str(BASE_DIR / "data" / "llm_usage.db"))
    # 图检查点存储（PR-3 / D2）：SqliteSaver 文件库自管建表，不动 MySQL 迁移（INV-3）
    checkpoint_db_path: str = Field(default_factory=lambda: str(BASE_DIR / "data" / "checkpoints.sqlite3"))
    # BYOK 网关密钥加密 key（Fernet urlsafe b64，解码须恰 32B）；空 = 从 JWT_SECRET 域分隔派生
    # （轮换 JWT_SECRET 会让存量密文失效，需在设置页重录）。
    byok_enc_key: str = ""

    # ---- 导出与上传 ----
    # PDF 导出（M6）：字体全仓唯一一份 simhei.ttf 已随 Java 退役迁入 app/resources/fonts/。
    # 部署可用 EXPORT_DIR / EXPORT_FONT_FILE 覆盖。
    export_dir: str = Field(default_factory=lambda: str(BASE_DIR / "data" / "export"))
    export_font_file: str = Field(default_factory=_default_font_path)
    # 封面/上传图落盘根目录（SPEC v2.3 §6.1）：DB 只存 /api/uploads/... 应用路径。
    uploads_dir: str = Field(default_factory=lambda: str(BASE_DIR / "data" / "uploads"))
    # 封面上传大小上限（SPEC v2.3 §6.3 / S0-3）：FastAPI 不限 multipart 体积，
    # 入口在 cover_service.read_upload_capped 流式截断，超限 413。
    cover_upload_max_bytes: int = 5 * 1024 * 1024

    # ---- 分享与调度 ----
    # 分享匿名访问限流（SPEC v2.3 §6.6 / E14）：按 IP 滑动窗口，每分钟上限
    share_rate_limit_per_minute: int = 60
    frontend_shell_url: str = ""  # 分享卡取壳地址（P1-5）：空=关闭；compose 按拓扑注入（见 share_card.py）
    # 匿名图片端点（/api/image-proxy、/api/poi-photo）按 IP 每分钟上限（R1-4）：无闸即能耗尽出网配额（client_ip.py）
    public_rate_limit_per_minute: int = 60
    schedule_optimizer_enabled: bool = True
    # MCP 出口（G-3.6）：默认关闭，管理员在后台开启后 /mcp 才可用
    mcp_enabled: bool = False
    # MCP 行程写入（C3.3）：默认关闭——查行程/重排一天/记账写工具单独显式打开
    mcp_write_enabled: bool = False
    # 模板广场（C2.4）：默认关闭，自托管私有部署不默认开放社区面
    template_community_enabled: bool = False
    # 条目对/错反馈（C3.5）：默认关闭——增强能力由管理员显式打开（与 mcp_write 同模式）
    item_feedback_enabled: bool = False

    # ---- 图片源 ----
    unsplash_access_key: str = ""
    # 图片多源解析的最后一级兜底图库。Pexels 对中文查询几乎无命中，
    # poi_photo 会先尝试把名称解析成英文再检索。
    pexels_access_key: str = ""
    poi_image_wiki: bool = True

    # ---- 外部地点层（POI 权威库退役后的坐标/分类/图片来源） ----
    # OpenTripMap（OSM+Wikidata 加工的旅游切片）：景点半径检索与详情；免费 key 在
    # https://opentripmap.io 注册（仅 en/ru，中文名由 LLM 对齐）。key 为空=整层禁用，生成降级纯 LLM+联网搜索。
    otm_api_key: str = ""
    # 景点半径检索范围与上限（城市尺度）
    otm_radius_m: int = 12000
    otm_limit: int = 40
    # OTM/Nominatim 单次调用超时（秒）；数据源失败一律静默降级，不阻断生成
    places_timeout_seconds: float = 8.0
    # Nominatim（OSM 官方地理编码）兜底：OTM 无法按"点名"解析，缺坐标点位经它补真实坐标；政策 1 rps。
    nominatim_enabled: bool = True
    # Open-Meteo 免 key 天气（C3.1）：研究证据与行程页逐日预报；失败静默，关掉即整体停用。
    weather_enabled: bool = True
    # UA 联系方式（R3-F7 拍板 2026-10-05）：免费源政策要求可联系的 User-Agent；真实值部署时填 .env 不入仓。
    ua_contact: str = "dev@localhost.invalid"

    # ---- 真实报价数据面（L12：Travelpayouts 主源 + SerpApi 按需补充源） ----
    # token 见 travelpayouts.com 后台；空 = 真价整层零外呼（维持 estimated 叙事）。
    # Aviasales 走 X-Access-Token 头（token 不进 URL）；Hotellook 缓存价端点已摘除（LA2）。
    travelpayouts_token: str = ""
    # SerpApi（Google Flights 实时价，免费档 250 次/月）：空 = 零外呼；仅按需触发。
    serpapi_key: str = ""
    # 进程内月配额（单机诚实口径：重启清零）。
    serpapi_monthly_quota: int = 250

    # ---- 存在性判定（PLAN-A1 G2；判"这个名字指的地点真的存在吗"） ----
    # 解析顺序（逗号分隔）：otm=OpenTripMap 池名匹配（只能正向证实）、nominatim=
    # 点名地理编码、amap/google_places=收费源留白（无 key 不发请求）。
    existence_provider_order: str = "otm,nominatim"
    # 单次生成 run 的存在性解析上限；耗尽后一律 UNKNOWN（绝不当成"不存在"）。
    # 实测一天行程约 30 个唯一点位名，24 覆盖主行程并给备选池留余量。
    existence_resolve_limit: int = 24
    # 位置一致性硬闸：命中点离目的地中心超过此半径即判"与本次行程矛盾"（out_of_area）。
    # 实测挡住的是「西安方所书店→安徽」这类跨城误配。
    existence_area_radius_m: int = 80000
    # 同一实体判定的字符重叠率下限（与固定最少共享字符数 4 配套）。
    # 0.55 是 2026-09-18 量测口径：降到 0.5 会放进「明婷小馆→报名大厅」。
    entity_name_similarity_min: float = 0.55
    # 备选池批量后验证的独立预算（后台富化，与主行程生成互不抢占额度）。
    # 一次产出的 suggestions 是 24-40 条，实测 40 条按 Nominatim 1 rps 约 45s。
    suggestion_resolve_limit: int = 40
    # 收费源留白（当前无 key）：配置后加进 existence_provider_order 即生效，
    # 空结果带 authoritative_negative=True，"证伪即删"随之自动生效。
    amap_web_key: str = ""
    google_places_api_key: str = ""

    # ---- MySQL ----
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "travel_assistant"
    # MySQL 连接池上限（Agent 侧只读知识库；管线写库共用同一池）
    db_pool_max: int = 10

    # ---- Redis ----
    # Redis Pub/Sub（AD2 SSE）：进度事件发布通道；尽力而为，连不上只降级。显式
    # REDIS_URL 优先，否则由 REDIS_HOST/REDIS_PORT 派生；默认 6380 对齐 start-all.ps1。
    redis_url: str = Field(default_factory=_default_redis_url)
    # 只在未显式给 REDIS_URL 时参与派生（compose 里 Redis 发布到宿主机端口，
    # 无口令等于把 JWT 吊销黑名单与登录锁桶交给整个局域网，见 R1-7）。
    redis_password: str = ""

    # ---- 鉴权 ----
    # 会话签名密钥（≥32 字符，启动强校验）；沿用双跑期与 Java 共票的键名约定。
    # 注意：DB 凭据与 compose 的 MYSQL_* 不同源，本服务读 DB_*。token 的 exp 写在
    # claims 里，与 Cookie Max-Age 的差异无害，可独立设置有效期。
    jwt_secret: str = ""
    jwt_expire_hours: int = 24
    # 登录按 IP 的总量滑窗（每分钟，审计 §3.3.3）：失败锁按 (IP, 用户名) 对计数，
    # 换用户名喷密码不触发；这道闸不看用户名不分成败，堵密码喷洒与 bcrypt CPU 放大。
    # 做成配置（而不是注册闸那类常量）：离线套件把它放开到极大值防止用例间互相
    # 污染（conftest `_neutralize_ip_and_user_limits`，登录在多个不 reset 状态的
    # 测试文件里都被调用），专项用例再改回小值。
    login_ip_rate_per_minute: int = 30
    # 默认安全：会话票是唯一凭据载体，明文链路上可被同网段摘取。本地明文 http 与
    # compose（nginx 只 listen 80）在 .env / compose env_file 里显式设 false，
    # validate_boot 为非回环绑定把关（见下）。
    auth_cookie_secure: bool = True
    jwt_revocation_prefer_redis: bool = Field(default=True, validation_alias="APP_JWT_REVOKE_PREFER_REDIS")

    @model_validator(mode="after")
    def _static_validation(self) -> "Settings":
        """配置静态自洽（数值/范围）：任何实例化都生效（含测试 import 期）。

        只做「配置自身自洽」的检查，且所有默认值必须能通过——依赖运行语境的安全
        检查（真实密钥、绑定地址）在 validate_boot()，放 import 期会炸掉测试与脚本。
        """
        bad = [
            f"{key}={getattr(self, key)} {requirement}"
            for key, requirement, _low, _high in NUMERIC_RULES
            if not numeric_ok(key, getattr(self, key))
        ]
        if bad:
            raise ValueError("配置项取值非法：" + "；".join(bad))
        return self

    def validate_boot(self) -> None:
        """main.py lifespan 的启动校验入口：坏配置 boot 期报清晰错误退出（不在 import 期）。"""
        # 占位密钥（审计 §3.4.2 / P0-5 应用侧）：公开示例值不能当真密钥——先于长度/组合
        # 校验拒绝，错误信息直指生成方式。空值不在此判：JWT_SECRET 空串由下面的长度
        # 校验拦，AGENT_INTERNAL_TOKEN 空串对本地回环开发是合法形态。
        for name, value in (("JWT_SECRET", self.jwt_secret), ("AGENT_INTERNAL_TOKEN", self.agent_internal_token)):
            mark = _secret_placeholder_mark(value)
            if mark is not None:
                raise RuntimeError(
                    f"{name} 仍是占位符串（{mark}…，来自 .env.example 的公开示例值）："
                    "公开值不能作密钥，请用 openssl rand -hex 32 生成真实随机串"
                )
        # 安全 fail-fast：generate/adjust 等端点每次调用消耗真实 LLM token。绑定非回环
        # 地址却不配置内部令牌，等于匿名烧钱接口；启动即拒绝，避免部署改 host 后裸奔。
        if self.agent_host not in LOCAL_HOSTS and not self.agent_internal_token:
            raise RuntimeError(
                "AGENT_HOST 绑定非回环地址但未配置 AGENT_INTERNAL_TOKEN，"
                "生成类端点将匿名暴露并消耗 LLM 配额；请设置令牌或改回 127.0.0.1"
            )
        # 会话签名密钥：默认空串必须拦下——那等于任何人都能伪造登录票。
        if len(self.jwt_secret) < 32:
            raise RuntimeError(
                "JWT_SECRET 未配置或短于 32 字符：本服务负责签发会话票，弱密钥可被伪造登录；"
                "请在 .env 里配置与（双跑期）Java 侧一致的密钥"
            )
        # R1-5：TRUSTED_PROXIES 里任何一项写错，app.common.client_ip 都会静默不信任那台
        # 代理（症状："所有访客共用代理 IP 一个限速桶"，倒不回配置），所以启动即拒。
        unparsable = [item.strip() for item in self.trusted_proxies.split(",") if item.strip() and not _proxy_ok(item)]
        if unparsable:
            raise RuntimeError(f"TRUSTED_PROXIES 含无法解析的条目：{unparsable}（支持 IP 或 CIDR，如 172.16.0.0/12）")
        # BYOK_ENC_KEY 配置了就必须是「解码后恰 32 字节」的 urlsafe base64——错配的 key
        # 只会让全部存量密文在运行期解不开，启动即拒（空值派生回落路径不受影响）。
        raw_byok = self.byok_enc_key.strip()
        if raw_byok:
            try:
                material = byok_key_material(raw_byok)
            except ValueError:
                material = b""
            if len(material) != 32:
                raise RuntimeError("BYOK_ENC_KEY 非法：须为解码后恰 32 字节的 urlsafe base64 串")
        # 明文链路上的会话票会被同网段摘取。compose 的 nginx 目前只 listen 80，
        # 所以这里不 fail-fast，只把风险写在启动日志里（改成 TLS 后请删掉显式 false）。
        if not self.auth_cookie_secure and self.agent_host not in LOCAL_HOSTS:
            logger.warning(
                "AUTH_COOKIE_SECURE=false 且绑定 %s：会话票将以明文 Cookie 传输，"
                "仅可接受于本地/内网可信链路；对外提供请上 TLS 并置 true",
                self.agent_host,
            )


settings = Settings()
