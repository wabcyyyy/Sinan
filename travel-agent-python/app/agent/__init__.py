"""Agent 层唯一公开面（G-1.2 边界收敛）。

约定：
- api/services 层只准 ``from app.agent import X`` 使用本导出面；深路径
  （``from app.agent.generation.orchestration.workflow import ...``）由 import-linter 门禁禁止；
- agent 层内部模块互相调用不受此限（``from app.agent.generation.orchestration.workflow import ...``
  在 agent 层内合法）；
- 新增对外能力 = 在对应子模块实现后，在此登记 re-export + ``__all__``；
- 真正跨模块共用的内部符号不走本门面，由所属模块去下划线提级为
  「跨模块 API」（见 generators/day_stream/tools/workflow 模块 docstring）。

依赖：本包各子模块；本文件不得承载业务逻辑。
"""

from app.agent.data.live_quotes import quota_exhausted as live_quote_quota_exhausted
from app.agent.data.weather import get_weather_forecast
from app.agent.editing.chat_draft import run_chat_turn
from app.agent.editing.chat_draft.confirm_graph import ConfirmResume, confirm_thread, resume_confirmation
from app.agent.editing.city_guide import run_city_guide
from app.agent.editing.clarify import run_clarify
from app.agent.editing.image_intent import run_image_intent
from app.agent.editing.local_replan import run_local_replan
from app.agent.editing.nl_edit import run_edit_ops
from app.agent.generation.content.butler import run_butler_note, run_poi_intros
from app.agent.generation.content.reflect import BUDGET_OVERAGE_MARK, validate_plans
from app.agent.generation.orchestration.day_stream import run_generate_day
from app.agent.generation.orchestration.plan_context import run_plan_context
from app.agent.generation.orchestration.stream_branch import run_generate_trip_stream
from app.agent.generation.orchestration.trip_graph import DayResume, resume_day
from app.agent.generation.orchestration.workflow import run_adjust, run_generate
from app.agent.generation.output.schedule_optimizer import optimize_daily_plan
from app.agent.grounding.existence import resolve_poi
from app.agent.grounding.grounding_labels import is_authoritative_source
from app.agent.grounding.suggestion_grounding import verify_suggestion_rows
from app.agent.runtime.observability import metrics, observe_run, scene, use_scene
from app.agent.runtime.usage_store import usage_store
from app.agent.tools.flight_quotes import search_live_flight_quotes, shape_quote_for_wire
from app.agent.tools.hotel_quotes import search_live_hotel_quotes
from app.agent.tools.impl import find_nearby_pois, get_poi_detail, search_hotels, workbench_search
from app.agent.tools.registry import ToolInvocationError, ToolSpec, registry

__all__ = [
    "BUDGET_OVERAGE_MARK",
    "ConfirmResume",
    "DayResume",
    "ToolInvocationError",
    "ToolSpec",
    "confirm_thread",
    "find_nearby_pois",
    "get_poi_detail",
    "get_weather_forecast",
    "is_authoritative_source",
    "live_quote_quota_exhausted",
    "metrics",
    "observe_run",
    "optimize_daily_plan",
    "registry",
    "resolve_poi",
    "resume_confirmation",
    "resume_day",
    "run_adjust",
    "run_butler_note",
    "run_chat_turn",
    "run_city_guide",
    "run_clarify",
    "run_edit_ops",
    "run_generate",
    "run_generate_day",
    "run_generate_trip_stream",
    "run_image_intent",
    "run_local_replan",
    "run_plan_context",
    "run_poi_intros",
    "scene",
    "search_hotels",
    "search_live_flight_quotes",
    "search_live_hotel_quotes",
    "shape_quote_for_wire",
    "usage_store",
    "use_scene",
    "validate_plans",
    "verify_suggestion_rows",
    "workbench_search",
]
