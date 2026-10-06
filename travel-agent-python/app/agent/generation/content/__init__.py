"""生成链路的内容层：造内容，外加被输出层与编排层共用的纯校验/事实。

- ``generators``：内容组装原语与对外面（纯函数优先，无编排）；
- ``suggestions``：备选池（候选组装、类别地板与上限、缺口补齐）；
- ``reference_pool``：参考资料池（权威候选 → 带编号资料 → 模型引用落地）；
- ``narrative``：叙事字段清洗（限值截断、类型降级、指令残留剥离）；
- ``day_prompts``：Prompt 组装与日/整段两种入口的 LLM 调用；
- ``landing``：两条生成链路共用的落地步骤（脏项过滤 → 引用/本地落地 → 证伪点删除）；
- ``butler``：管家式讲解与景点详细介绍生成；
- ``reflect``：生成结果的自我校验（纯规则检查行程是否合理）；
- ``route_matrix``：按日路线时间矩阵（reflect 与输出层共用）；
- 图状态单一定义在 ``app.agent.research.agent_state``（UnifiedAgentState，PR-2 State 单一化）：
  编排层与输出层共用的纯结构，本模块原有的 ``graph_state`` 已并入该定义。

后三个之所以在本层而不在 ``orchestration``：它们被 ``output``（装配/终检）与
``orchestration`` 同时消费，必须落在 ``output`` 之下，否则阶梯会反向。

依赖只准向下（``rules`` 与下层域）；不得 import ``output`` / ``orchestration``。
"""
