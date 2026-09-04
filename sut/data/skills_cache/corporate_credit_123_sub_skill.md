---
name: corporate_credit_123_sub_skill
description: >
  子Agent专用Skill：单企业信贷123分析（步骤1串行 + 步骤2/3/4并行）。
  
  执行流程：
  1. 步骤1：调用 call_versatile 获取企业基础信息（baseInfo）
  2. 步骤2/3/4：拿到 baseInfo 后，调用 call_multiversatile 并行执行3个工作流
     - 企业综合分析
     - 金融服务方案推荐
     - 尽职调查清单
  
  由主Agent通过 call_multiagent 调度，不直接响应用户请求。
  
  前置条件：必须已确认 customer_name（客户名称）。
---

# corporate_credit_123_sub_skill

## 职责

作为子Agent的执行Skill，接收主Agent分发的单企业分析任务，先串行执行步骤1获取baseInfo，再并行执行步骤2/3/4完成分析。

## 工具白名单

- `call_versatile`（步骤1：信息提取）
- `call_multiversatile`（步骤2/3/4：并行工作流）

## 执行流程

### 第一步：确认客户名称

从主Agent分发的 `entity_name` 字段直接获取企业名称作为 `customer_name`。若缺失，返回错误信息。

### 第二步：调用接口-基础企业信息检索（步骤1）

调用第一个接口获取企业基础数据：

```
call_versatile(
  query_description="{customer_name}",
  query_intent="基本信息抽取"
)
```

**参数说明：**
- `query_description`：企业名称，用于工作流查询
- `query_intent`：业务意图，固定为 `"基本信息抽取"`，用于路由到对应的工作流

工具返回结构：
```json
{
  "status": "success",
  "baseInfo": "..."
}
```

**关键**：LLM 需将 `baseInfo` 暂存于 thought 中，用于步骤2/3/4的并行调用。

### 第三步：并行调用3个工作流（步骤2/3/4）

拿到 baseInfo 后，**一次性**调用 `call_multiversatile` 并行执行3个工作流：

```
call_multiversatile(
  workflows=[
    {
      "query_intent": "信贷综合金融",
      "query": "$${customer_name}$${baseInfo}$$"
    },
    {
      "query_intent": "信贷综合分析",
      "query": "$${customer_name}$${baseInfo}$$"
    },
    {
      "query_intent": "尽调要点",
      "query": "$${customer_name}$${baseInfo}$$"
    }
  ]
)
```

**注意**：
- 3个工作流必须**一次性并行调用**，不得串行
- `query` 格式：`$${customer_name}$${baseInfo}$$`

### 第四步：输出结果

等待3个工作流全部返回后，按以下格式输出：

```
公司信贷综合全面分析报告 - {customer_name}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

一、企业综合分析

{步骤2结果}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

二、金融服务方案推荐

{步骤3结果}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

三、尽职调查清单

{步骤4结果}
```

## query 格式规范

| 步骤 | query_intent | query 格式 |
|------|--------------|------------|
| 步骤1 | 基本信息抽取 | `{customer_name}` |
| 步骤2 | 信贷综合金融 | `$${customer_name}$${baseInfo}$$` |
| 步骤3 | 信贷综合分析 | `$${customer_name}$${baseInfo}$$` |
| 步骤4 | 尽调要点 | `$${customer_name}$${baseInfo}$$` |

## 强约束

1. **步骤顺序**：必须先完成步骤1获取 baseInfo，再执行步骤2/3/4
2. **并行要求**：步骤2/3/4 必须通过 `call_multiversatile` **一次性并行调用**，禁止串行
3. **数据完整性**：步骤2/3/4 的 task_description 中必须包含步骤1返回的 baseInfo，不得编造
4. **禁止递归**：本 Skill 不可调用 `call_multiagent`
5. **结果格式**：必须按第四步格式输出，便于主Agent汇总

## 终态回复模板

- 成功：
  ```
  公司信贷综合全面分析报告 - {customer_name}
  
  [按第四步格式输出]
  ```

- 步骤1失败：`企业信息提取失败：{fail_cause}`
- 步骤2/3/4部分失败：`部分工作流执行失败：{失败的工作流列表}`
- 缺槽位：`缺少必要信息（客户名称），无法进行公司信贷综合分析`

## 与主Agent Skill 的协作

| 角色 | Skill | 职责 |
|------|-------|------|
| 主Agent | `corporate_credit_123_multi_skill` | 识别多实体，并行调度子Agent |
| 子Agent | `corporate_credit_123_sub_skill`（本Skill） | 单企业分析：步骤1串行 + 步骤2/3/4并行 |

> 本 Skill 输出仍受 `AgentRule §B 硬契约` 与 `§C 默认输出风格` 约束（不重复 / 不透传 JSON / 不编造数据）。
