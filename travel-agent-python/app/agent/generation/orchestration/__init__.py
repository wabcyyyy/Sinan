"""生成链路的编排层（最上层）：图、循环、流式链路与对外门面。

- ``workflow`` / ``trip_graph`` / ``graph_nodes``：统一生成图（整段与单日共用一张）与节点实现；
- ``day_workflow`` / ``day_stream``：单日链路的门面与一次生成的编排+落地；
- ``trip_stream`` / ``stream_parser``：整段流式（边流边解析逐天落地）；
- ``open_plans``：开放模式的逐日草案与降级；
- ``plan_context``：生成上下文构建（研究证据收集与进度事件）。

``workflow`` / ``trip_graph`` / ``day_workflow`` / ``day_stream`` 之间用懒导入互调
（薄门面破环），因此必须同层——把它们拆到不同层会让阶梯契约与真实代码打架。

本层调用 ``output`` 把行程装配落地：import 方向是编排 → 输出（执行顺序上输出在后）。
"""
