"""生成链路域：造行程内容。域内分四层，自下而上。

```
downstream      终检 / 输出落地 / 时间窗优化：formatting/（价格·事实·质量）· critic · schedule_optimizer
orchestration   编排：图与循环（workflow / trip_graph / graph_nodes / day_workflow）·
                两条流式链路（trip_stream / day_stream / stream_parser）·
                逐日草案（open_plans）· 校验（reflect / route_matrix）· 上下文（plan_context / graph_state）
content         造内容：generators · suggestions · reference_pool · narrative · day_prompts ·
                landing · butler
rules           纯规则：generation_core · budget（不 import 兄弟层）
```

阶梯位置：``research`` 之上、``editing`` 之下。域内四层**也是单向**的：上层可 import 下层，
下层不得 import 上层——机检 = pyproject 的 "Generation sublayers" 契约。

几条跨层的既有事实（别当成倒挂）：
- ``reflect`` 在 orchestration 而非 downstream：``day_stream`` 生成一天后立即校验；
- ``workflow`` / ``trip_graph`` / ``day_workflow`` / ``day_stream`` 之间存在薄门面互相
  调用（懒导入破环），所以它们必须同层；
- ``formatting`` 与 ``critic`` 是 workflow 的**下游**，不得反过来 import 造内容模块。

细则（每模块一句话职责、依赖方向、"新增生成能力先抄谁"）见 ``app/agent/README.md``。

不设 re-export 面：域内符号按子模块路径导入。
"""
