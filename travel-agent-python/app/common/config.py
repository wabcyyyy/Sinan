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

依赖：python-dotenv、pydantic-settings；内部仅 app.common.config_rules（数值范围规则表）
与 app.common.config_primitives（回环/占位符/代理网段的形态判定），两者都是本文件拆出的纯判定面。
"""

import base64
import binascii
import logging
import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.common.config_primitives import LOCAL_HOSTS, proxy_ok, secret_placeholder_mark
from app.common.config_rules import NUMERIC_RULES, numeric_ok

BASE_DIR = Path(__file__).resolve().parent.parent.parent
# 先加载仓库根目录共享 .env，再加载本模块 .env（同名变量后者优先）
load_dotenv(BASE_DIR.parent / ".env")
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)


def _get(name: str, default: str) -> str:
    """仅供本模块 default_factory 派生默认值使用（INV-7 边界内的环境读取）。"""
    return os.getenv(name, default)


def _default_font_path() -> str:
    """随仓 OFL 中文静态字体；许可与固定来源见字体目录。"""
    return str(BASE_DIR / "app" / "resources" / "fonts" / "SinanSansSC-Regular.ttf")


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
    registration_invite_required: bool = False
    user_generation_limit_enabled: bool = False
    nominatim_base_url: str = "https://nominatim.openstreetmap.org/search"
    trial_billing_enabled: bool = False
    trial_initial_credit_cny: float = 6.0
    llm_price_cny_json: str = "{}"
    serve_frontend: bool = False
    frontend_dist: str = "frontend"
    port: int = 8000
    railway_edge_headers: bool = False
    agent_reload: bool = False
    agent_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    agent_internal_token: str = ""
    agent_deadline_seconds: float = 240
    itinerary_deadline_seconds: float = 300
    research_deadline_seconds: float = 60
    # 规划任务（V16/T1）：心跳与每次预算写入都续租，租约过期即视为 worker 已死、可被接管，
    # 旧 worker 的条件更新同时失效。90 = 3 个心跳周期；探针缓存只服务无预算变化的纯写路径。
    planning_live_seconds: float = 90
    planning_heartbeat_seconds: float = 30
    planning_write_probe_seconds: float = 1
    planning_worker_concurrency: int = 4
    planning_queue_capacity: int = 8
    planning_poll_seconds: float = 1
    planning_health_path: str = ""
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
    # main 备选链与通道告警（审计 P0-2；完整语义见 model_registry / llm_breaker 与 .env.example）
    llm_main_fallback_provider: str = ""
    llm_alert_webhook_url: str = ""
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
    # 默认留空：只在显式配置的 ORS 实例上查路，不自动调用托管 API。
    ors_base_url: str = ""
    ors_api_key: str = ""
    ors_timeout_seconds: float = 4
    ors_region: str = ""
    # FOSSGIS 布局的 OSRM（如 https://routing.openstreetmap.de）；仅显式启用。
    osrm_base_url: str = ""
    osrm_timeout_seconds: float = 8

    # ---- Agent 运行预算 ----
    tool_max_calls: int = 32
    max_llm_calls: int = 32
    max_token_budget: int = 80000
    # 按用户的闸门（R1-9）：上面两份预算是"按次"的，单用户循环调用拿到的就是
    # N 份按次额度 → 无上限；这两项才是按 principal 的顶。
    user_llm_runs_per_minute: int = 6
    user_daily_llm_runs: int = 20
    # P1-8 同族两项：实时价两端点直烧 SerpApi 共享月池（按人闸门）；agent 直调面
    # 按 token 的进程内分钟窗（token 泄露止损，内网信任面 + 单进程前提）
    user_live_quotes_per_minute: int = 6
    agent_rate_limit_per_minute: int = 30
    # ---- 成本硬顶（V17/T2）：账本在 MySQL（cost_ledger），不在 Redis ----
    # 单位一律是 token 与调用次数：本仓没有可信定价表，编造人民币金额只会造出一份
    # "看着精确、实则无人负责"的账。没有可信定价时用 token 硬顶（PLAN §5 T2）。
    # 默认值按"公开上线首日"取保守量级，真实单位成本出来后再收紧（T6/T9 的实测项）。
    # 0 = 该维度不限（自托管部署关掉窗口顶的合法形态；预留/结算/收口不变式不受影响）。
    user_daily_token_cap: int = 8000000
    user_daily_call_cap: int = 5000
    platform_daily_token_cap: int = 20000000
    platform_daily_call_cap: int = 20000
    # 一笔预留的结算宽限下限：实际宽限取 max(LLM_TIMEOUT+30, 本值)。到期仍 OPEN 的预留
    # 由清理任务收口为 ABANDONED——额度**保留**（保守），且从此不可再结算（幂等的另一半）。
    cost_reservation_ttl_seconds: float = 270
    # 同一窗口允许同时悬挂的未结算预留数：崩溃循环的有界性判据（不依赖清理任务准时跑到）。
    # 上限按"并发腿数"量级给：一次生成最多研究三路 + 生成 + 修复并行，64 足够且不掩盖真卡死。
    cost_max_open_reservations: int = 64
    # 行程级累计补齐额度（跨 attempt、跨用户点击只增）：免扣的是用户"每日生成次数"，
    # 不免扣补齐额度（P0-3 产品语义 + PLAN §5 T2 新增项）。0 = 不限（保留旧行为）。
    trip_repair_allowance: int = 6
    repair_backoff_base_seconds: float = 30
    repair_backoff_max_seconds: float = 1800

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
    # PDF 导出：随仓 Sinan Sans SC（派生自 Noto，OFL），不依赖宿主机字体。
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
    # 公开行程库：默认启用，只展示主动发布的快照；私有部署可关闭。
    template_community_enabled: bool = True
    # 条目对/错反馈（C3.5）：默认关闭——增强能力由管理员显式打开（与 mcp_write 同模式）
    item_feedback_enabled: bool = False
    # 出发前照看（个人 Agent V1）：详情页「出发前照看」面板与后台天气检查的总开关。
    # false 时 care 读端点报 supported=false、写端点 404；开启后仍按用户级开关逐行程启用。
    care_enabled: bool = False

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
    overpass_enabled: bool = True  # Overpass（OSM diet 标签检索）：饮食证据餐饮池；免费无 key，失败静默降级
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
    # 按 IP 总量限制登录，补充按 (IP, 用户名) 的失败锁。
    login_ip_rate_per_minute: int = 30
    # 默认安全：会话票是唯一凭据载体，明文链路上可被同网段摘取。本地明文 http 与
    # compose（nginx 只 listen 80）在 .env / compose env_file 里显式设 false，
    # validate_boot 为非回环绑定把关（见下）。
    auth_cookie_secure: bool = True
    jwt_revocation_prefer_redis: bool = Field(default=True, validation_alias="APP_JWT_REVOKE_PREFER_REDIS")

    @model_validator(mode="after")
    def _static_validation(self) -> "Settings":
        """校验数值自洽；依赖真实密钥、绑定地址的检查仅在 validate_boot 执行。"""
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
        if self.serve_frontend and not self.auth_cookie_secure:
            raise RuntimeError("公开托管入口必须设置 AUTH_COOKIE_SECURE=true")
        # 占位密钥（审计 §3.4.2 / P0-5 应用侧）：公开示例值不能当真密钥——先于长度/组合
        # 校验拒绝，错误信息直指生成方式。空值不在此判：JWT_SECRET 空串由下面的长度
        # 校验拦，AGENT_INTERNAL_TOKEN 空串对本地回环开发是合法形态。
        for name, value in (("JWT_SECRET", self.jwt_secret), ("AGENT_INTERNAL_TOKEN", self.agent_internal_token)):
            mark = secret_placeholder_mark(value)
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
        if len(self.jwt_secret) < 32:
            raise RuntimeError(
                "JWT_SECRET 未配置或短于 32 字符：本服务负责签发会话票，弱密钥可被伪造登录；"
                "请在 .env 里配置与（双跑期）Java 侧一致的密钥"
            )
        unparsable = [item.strip() for item in self.trusted_proxies.split(",") if item.strip() and not proxy_ok(item)]
        if unparsable:
            raise RuntimeError(f"TRUSTED_PROXIES 含无法解析的条目：{unparsable}（支持 IP 或 CIDR，如 172.16.0.0/12）")
        raw_byok = self.byok_enc_key.strip()
        if raw_byok:
            try:
                material = byok_key_material(raw_byok)
            except ValueError:
                material = b""
            if len(material) != 32:
                raise RuntimeError("BYOK_ENC_KEY 非法：须为解码后恰 32 字节的 urlsafe base64 串")
        if not self.auth_cookie_secure and self.agent_host not in LOCAL_HOSTS:
            logger.warning(
                "AUTH_COOKIE_SECURE=false 且绑定 %s：会话票将以明文 Cookie 传输，"
                "仅可接受于本地/内网可信链路；对外提供请上 TLS 并置 true",
                self.agent_host,
            )


settings = Settings()
