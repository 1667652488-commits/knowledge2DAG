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

# 业务范围声明
scope:
  allowed:
    - "多企业并行分析"
    - "多实体风险评估"
    - "多企业对比报告"
    - "批量企业信息查询"
    - "单企业分析"
    - "单实体业务操作"
  denied: []

# 专属工具声明（agent.py 按此注册 call_multiagent + MultiagentInterruptRail）
# call_versatile 为通用工具，始终注册，无需在此声明
tools:
  - "call_multiagent"

# todolist 业务步骤目录（主 Agent 场景：先全称确认，再用 corporate_credit_123_multi_skill 处理多实体分析）
todolist_steps:
  - step_id: 1
    content: "识别用户请求中的公司，若为简称/俗称/歧义名则确认完整工商全称"
    skill: "customer_confirm_skill"
  - step_id: 2
    content: "基于确认后的完整公司名称，识别业务实体"
    skill: "corporate_credit_123_multi_skill"
  - step_id: 3
    content: "为每个实体生成子任务描述"
    skill: "corporate_credit_123_multi_skill"
  - step_id: 4
    content: "并行调度子 Agent 执行"
    skill: "corporate_credit_123_multi_skill"
  - step_id: 5
    content: "汇总各子 Agent 结果，生成综合分析报告"
    skill: "corporate_credit_123_multi_skill"

# Skill 路由规则（单企业/多企业统一走子Agent路径）
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

0. **全称确认前置**：当用户请求中的公司名为简称/俗称/歧义名（如"小米"）时，主 Agent 必须先进入 `customer_confirm_skill` 完成全称确认：
   - 以 `query_description={简称}`、`query_intent="公司全称识别"` 调用 `call_versatile`，从返回的 `workflow_result` 中提取完整工商注册全称；
   - 再用 `ask_user`（仅 `question` 兜底文本，不传 `response_template_*`）向用户确认该全称；
   - 用户确认后，完整公司名称作为 `customer_full_name` 槽位暂存，传入后续 skill 的 `query_description` 执行具体意图（分层分类/信贷分析等）；
   - 用户已提供完整工商全称时，跳过此步，直接进入实体识别。

   字段约定：
   - `query_description`：传给 `call_versatile` 的公司名称（简称或确认后的全称）
   - `query_intent`：业务意图路由标识（全称确认步固定为 `"公司全称识别"`）
   - `customer_full_name`：经用户确认的完整工商注册全称（Skill 内部槽位变量，非工具参数，供后续 skill 的 `query_description` 使用）

1. **实体识别**：完成全称确认后（或用户已提供完整全称），当用户请求涉及业务实体时，必须使用 `call_multiagent` 工具调度子 Agent 执行，`entities` 中的 `entity_name` 使用确认后的 `customer_full_name`
2. **单实体也走子Agent**：当用户请求仅涉及单个实体时，也使用 `call_multiagent` 工具调度1个子Agent执行（entities 数组包含1个元素），主Agent**禁止**直接调用 `call_versatile`（子Agent内部使用 `call_versatile` + `call_multiversatile` 是正常的）
3. **实体拆分**：每个实体必须包含 `entity_id`（固定填占位符 `entity_auto`，由系统自动生成唯一标识，LLM 无需自行编造）、`entity_name`（中文名称）、`entity_type`（实体类型，用于映射子Agent）、`query`（子任务描述，格式："对{entity_name}进行公司信贷123信息提取"）
4. **结果汇总**：所有子 Agent 执行完成后，主 Agent 负责汇总各实体结果，生成综合分析报告
5. **失败不重试**：当 `call_multiagent` 返回失败（`status=failed`/`partial_success`/空结果/子 Agent 执行异常）时，主 Agent **禁止**重新调用 `call_multiagent`，也**禁止**转而调用 `call_versatile` 尝试单独执行。必须直接基于已有返回结果（含失败原因）生成最终汇总报告，向用户如实展示哪些实体分析失败及失败原因，并提示用户可稍后重试或调整请求

## 调用示例

```json
{
  "entities": [
    {"entity_id": "entity_auto", "entity_name": "小米科技", "entity_type": "ZDT", "query": "对小米科技进行公司信贷123信息提取"},
    {"entity_id": "entity_auto", "entity_name": "华为技术", "entity_type": "ZDT", "query": "对华为技术进行公司信贷123信息提取"}
  ]
}
```
