---
# ════════════════════════════════════════════════════
# AgentRule_task_dispatch — 多实体任务分发场景配置
# 主 Agent 场景：识别多实体 → 并行调度子 Agent
# ════════════════════════════════════════════════════

name: task_dispatch_agent
description: "主 Agent 场景：识别用户请求涉及业务实体，统一通过 call_multiagent 调度子 Agent 执行分析（单实体/多实体均走子Agent路径）"

# Agent 信息
agent_name: "EDPAgent"
agent_description: "企业数据分析主 Agent，负责多实体任务规划和并行调度"


# 专属工具声明（agent.py 按此注册 call_multiagent + MultiagentInterruptRail）
# call_versatile 为通用工具，始终注册，无需在此声明
tools:
  - "call_multiagent"
  - "call_multiversatile"

# todolist 业务步骤目录（主 Agent 场景：先全称确认，再用 corporate_credit_123_multi_skill 处理多实体分析）
todolist_steps:
  - step_id: 1
    content: "基于确认后的公司名称，识别业务实体"
    skill: "corporate_credit_123_multi_skill"
  - step_id: 2
    content: "为每个实体生成子任务描述"
    skill: "corporate_credit_123_multi_skill"
  - step_id: 3
    content: "并行调度子 Agent 执行"
    skill: "corporate_credit_123_multi_skill"
  - step_id: 4
    content: "汇总各子 Agent 结果，生成综合分析报告"
    skill: "corporate_credit_123_multi_skill"

# Skill 路由规则（单企业/多企业均路由到 corporate_credit_123_multi_skill，由 Skill 内部按实体数量分流）
skill_routing:
  - trigger: "企业分析"
    skill: "corporate_credit_123_multi_skill"
    priority: 1

# 工具调用架构
architecture:
  type: "multiagent_parallel"
  description: "主 Agent 识别多实体，通过 call_multiagent 并行调度子 Agent"
  applicable_skills:
    - "customer_confirm_skill"
    - "corporate_credit_123_multi_skill"
---

# 多实体任务分发规则

## 核心规则

1. **实体识别**：完成全称确认后（或用户已提供完整全称），当用户请求涉及业务实体时，必须使用 `call_multiagent` 工具调度子 Agent 执行，`entities` 中的 `entity_name` 使用确认后的 `customer_full_name`
2. **单实体不走子Agent**：当用户请求仅涉及单个实体时，主Agent直接调用 `call_versatile` + `call_multiversatile` 是正常的）
3. **实体拆分**：每个实体必须包含 `entity_id`（固定填占位符 `entity_auto`，由系统自动生成唯一标识，LLM 无需自行编造）、`entity_name`（中文名称）、`entity_type`（实体类型，用于映射子Agent）、`query`（子任务描述，格式："对{entity_name}进行公司信贷123信息提取"）


