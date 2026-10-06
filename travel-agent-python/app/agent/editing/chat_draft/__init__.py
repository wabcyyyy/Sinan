"""chat_draft：把用户自然语言改行程的请求，产出一份可确认的安全草稿。

本包按"关注点"分为：intent（意图/天数解析）、validate（安全校验）、
document（JSON 投影与补水）、plan_edit（编辑执行层）、hotel（酒店子系统）、
decide（编排与对外入口）。核心范式：LLM 当规划者产出结构化意图，确定性代码当
执行者落地并校验，最终由前端确认后才落库。

门面只导出 `run_chat_turn`（`app/agent/__init__.py` 唯一需要的对外能力）。
包内其余符号一律**按子模块路径**导入：这里此前有一份 74 个符号的 re-export 壳，
理由写着"保持与原单文件模块的导入兼容"，但全仓只有 1 个测试穿过它，而它需要
`pyproject.toml` 里一条无期限的 F401 豁免才能不报未用导入 —— 既违反"接缝就地
迁移、不加包装层"，也违反"豁免必须留期限"。壳已删，豁免已撤。
"""

from .decide import run_chat_turn

__all__ = ["run_chat_turn"]
