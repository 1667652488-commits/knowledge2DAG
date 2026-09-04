---
# ════════════════════════════════════════════════════
# AgentRule_zhidai — 智贷通场景配置
# 子 Agent 场景：接收主 Agent 分发的任务，并行调用多工作流
# ════════════════════════════════════════════════════

name: zdt_agent
description: "子 Agent 场景：接收主 Agent 分发的实体分析任务，并行调用多个工作流完成分析"

# Agent 信息
agent_name: "SubEDPAgent"
agent_description: "智贷通子 Agent，负责单实体的多维度分析，并行调用工作流"

  denied:
    - "跨实体操作（由主 Agent 处理）"

# 专属工具声明（agent.py 按此注册 call_multiversatile + 配套 Rail）
# 注意：call_versatile 是通用工具，始终会被注册，无需在此声明
tools:
  - "call_multiversatile"  # 步骤2/3/4：并行工作流

# todolist 业务步骤目录（子 Agent 场景使用 corporate_credit_123_sub_skill 处理单实体分析）
todolist_steps:
  - step_id: 1
    content: "分析实体任务，拆分为多个工作流意图"
    skill: "corporate_credit_123_sub_skill"
  - step_id: 2
    content: "并行调用多个工作流执行分析"
    skill: "corporate_credit_123_sub_skill"
  - step_id: 3
    content: "汇总工作流结果，生成实体分析报告"
    skill: "corporate_credit_123_sub_skill"

# Skill 路由规则
skill_routing: []

# 工具调用架构
architecture:
  type: "multiversatile_parallel"
  description: "子 Agent 拆分任务为多个工作流意图，通过 call_multiversatile 并行调用"
---

# 智贷通子 Agent 规则

## 核心规则

1. **意图拆分**：将主 Agent 分发的任务拆分为多个工作流意图（基本信息抽取、信贷综合金融、信贷综合分析、尽调要点）
2. **并行调用**：使用 `call_multiversatile` 工具并行调用多个工作流，每个工作流包含 `query_intent` 和 `query`
3. **单意图回退**：当仅需调用单个工作流时，使用 `call_versatile` 工具按原有流程处理
4. **结果汇总**：所有工作流执行完成后，汇总结果生成实体分析报告返回给主 Agent

## 调用示例

```json
{
  "workflows": [
    {"query_intent": "信贷综合金融", "query": "$${customer_name}$${补充数据或空}$${附件路径或空}$${baseInfo}$$"},
    {"query_intent": "信贷综合分析", "query": "$${customer_name}$${补充数据或空}$${附件路径或空}$${baseInfo}$$"},
    {"query_intent": "尽调要点", "query": "$${customer_name}$${补充数据或空}$${附件路径或空}$${baseInfo}$$"}
  ]
}
```
