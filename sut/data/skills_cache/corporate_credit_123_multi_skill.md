---
name: corporate_credit_123_multi_skill
description: >
  企业信贷123分析智能体（单企业/多企业统一）。采用两级并行架构：
  
  第一级并行：主Agent识别企业实体，通过 call_multiagent 调度子Agent执行
  第二级并行：每个子Agent拿到baseInfo后，并行调用3个工作流（综合分析、融资方案、尽调清单）
  
  单企业场景：entities 数组包含1个元素，子Agent内部先串行执行步骤1获取baseInfo，再并行执行步骤2/3/4
  多企业场景：entities 数组包含N个元素，N个子Agent并行执行
  
  触发词：公司信贷、企业分析、信贷123、批量企业分析、多企业信贷分析、并行企业尽调。
  
  前置条件：用户请求中必须包含企业名称。
---

# corporate_credit_123_multi_skill

## 职责

识别用户请求中的企业实体（单企业或多企业），通过 `call_multiagent` 将任务调度给子 Agent。子 Agent 收到任务后会自行执行：
1. 步骤1：调用 `call_versatile(query_description="{customer_name}", query_intent="基本信息抽取")` 获取企业基础信息
2. 步骤2/3/4：调用 `call_multiversatile` 并行执行3个工作流

## 工具白名单

- `call_multiagent`

## 两级并行架构

```
┌─────────────────────────────────────────────────────────────┐
│ 主Agent（本Skill）                                           │
│ 识别N个企业 → call_multiagent(entities=[企业1,企业2,...,企业N])│
│             ↓ 仅调度，不执行工作流                            │
└─────────────────────────────────────────────────────────────┘
                           ↓ 并行调度（无工作流调用）
    ┌──────────────────────┼──────────────────────┐
    ↓                      ↓                      ↓
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│ 子Agent1    │      │ 子Agent2    │      │ 子AgentN    │
│ 企业1       │      │ 企业2       │      │ 企业N       │
└─────────────┘      └─────────────┘      └─────────────┘
    ↓                      ↓                      ↓
call_versatile         call_versatile         call_versatile
(intent="基本信息抽取",   (intent="基本信息抽取",   (intent="基本信息抽取",
 query="企业1")         query="企业2")         query="企业N")
    ↓                      ↓                      ↓
步骤1: 获取baseInfo     步骤1: 获取baseInfo     步骤1: 获取baseInfo
    ↓                      ↓                      ↓
call_multiversatile    call_multiversatile    call_multiversatile
    ↓                      ↓                      ↓
┌─────────────────────────────────────────────────────────────┐
│ 第二级并行：3个工作流                                         │
│  workflows=[                                               │
│    {query_intent: "信贷综合金融", query: "..."},            │
│    {query_intent: "信贷综合分析", query: "..."},            │
│    {query_intent: "尽调要点", query: "..."}                 │
│  ]                                                         │
└─────────────────────────────────────────────────────────────┘
```

## 工作流 Intent 配置

子 Agent 执行时调用的工作流及其 intent 映射：

| 步骤 | 工作流名称 | query_intent | 说明 |
|------|-----------|--------------|------|
| 步骤1 | 企业基础信息检索 | `基本信息抽取` | 获取企业基础信息（baseInfo） |
| 步骤2 | 信贷综合金融 | `信贷综合金融` | 企业综合分析 |
| 步骤3 | 信贷综合分析 | `信贷综合分析` | 金融服务方案推荐 |
| 步骤4 | 尽调要点 | `尽调要点` | 尽职调查清单 |

## 输入槽位

从用户请求中提取：
- `entities`：企业实体列表，每项包含：
  - `entity_id`：**唯一数字ID**（用于 conversation 传递给工作流，格式：`entity_001`、`entity_002`...）
  - `entity_name`：企业名称
  - `entity_type`：实体类型（用于映射子Agent，固定为 `"ZDT"`）
  - `query`：子任务描述（格式：`"对{企业名称}进行公司信贷123信息提取"`，需动态拼接企业名称）

## 执行流程

### 第一步：实体识别

从用户输入中识别所有企业名称。例如：
- 用户输入："请对OPPO、荣耀、大疆等公司进行信贷123"
- 识别结果：["OPPO", "荣耀", "大疆"]

### 分流判断（关键）

根据识别到的企业数量选择执行路径：

- **单企业（1家）**：主 Agent 直接执行，跳到「单企业执行流程」
- **多企业（≥2家）**：按下方「第二/三/四步」构建 entities 数组并调用 `call_multiagent`

#### 单企业执行流程

当仅识别到1家企业时，主 Agent 直接调用工作流，**不使用 call_multiagent**：

1. **步骤1**：`call_versatile(query_description="{企业名称}", query_intent="基本信息抽取")` → 获取 baseInfo
2. **步骤2/3/4 并行**：`call_multiversatile(workflows=[{"query_intent": "信贷综合金融", "query": "..."}, {"query_intent": "信贷综合分析", "query": "..."}, {"query_intent": "尽调要点", "query": "..."}])` → 基于 baseInfo 并行执行3个工作流
3. **汇总**：按第四步格式输出单企业分析报告

### 第二步：构建 entities 数组（仅多企业场景）

为每个企业构建实体描述，query 需动态拼接企业名称，entity_name 为企业名称，子Agent会根据此query执行步骤1信息提取，然后自动触发步骤2/3/4并行：

```json
{
  "entities": [
    {"entity_id": "entity_001", "entity_name": "小米科技", "entity_type": "ZDT", "query": "对小米科技进行公司信贷123信息提取"},
    {"entity_id": "entity_002", "entity_name": "华为技术", "entity_type": "ZDT", "query": "对华为技术进行公司信贷123信息提取"},
    {"entity_id": "entity_003", "entity_name": "蔚来汽车", "entity_type": "ZDT", "query": "对蔚来汽车进行公司信贷123信息提取"}
  ]
}
```

### 第三步：调用 call_multiagent

```
call_multiagent(entities=[...])
```

系统将并行调度子 Agent，每个子 Agent 执行：
1. **步骤1**：信息提取 → 获得 baseInfo
2. **步骤2/3/4 并行**：基于 baseInfo 并行调用3个工作流

### 第四步：汇总结果

等待所有子 Agent 返回结果后，按以下格式汇总输出：

```
多企业信贷123综合分析报告

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【企业1】{entity_name}

一、企业综合分析
{步骤2结果}

二、金融服务方案推荐
{步骤3结果}

三、尽职调查清单
{步骤4结果}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【企业2】{entity_name}
...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

汇总说明：
- 成功分析企业数：{success_count}
- 失败企业数：{failed_count}
- 失败企业列表：{failed_entities}（如有）
```

## 强约束

1. **实体数量限制**：单次并行调用不超过 5 家企业，超出时需向用户说明并分批处理
2. **单实体也走子Agent**：当用户请求仅涉及单家企业时，也使用 `call_multiagent` 调度1个子Agent执行（entities 数组包含1个元素），主Agent**禁止**直接调用 `call_versatile` 串行执行4个步骤（子Agent内部使用 `call_versatile` + `call_multiversatile` 是正常的）
3. **禁止递归**：本 Skill 不可在 cascade 续轮中再次调用 `call_multiagent`
4. **结果完整性**：必须等待所有子 Agent 返回结果后再输出，不得提前截断
5. **失败处理**：部分子 Agent 失败时，汇总报告中需明确标注失败企业及原因

## 终态回复模板

- 成功（全部子 Agent 返回）：
  ```
  多企业信贷123综合分析报告
  
  [按第四步格式输出各企业分析结果]
  ```

- 部分失败：
  ```
  多企业信贷123综合分析报告（部分失败）
  
  [成功企业的分析结果]
  
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  
  以下企业分析失败：
  - {entity_name}：{失败原因}
  ```

- 全部失败：`多企业信贷分析失败：所有子 Agent 执行失败，请稍后重试`

- 未识别到企业：`未从请求中识别到企业名称，请明确指定需要分析的企业`

## 与单实体 Skill 的协作

| 场景 | 使用 Skill |
|------|-----------|
| 单家企业分析 | `corporate_credit_123_multi_skill`（本 Skill，主Agent直接调 call_versatile + call_multiversatile） |
| 多家企业分析 | `corporate_credit_123_multi_skill`（本 Skill，主Agent调 call_multiagent 调度子Agent） |

单企业由主Agent直接执行4步工作流（call_versatile + call_multiversatile）；多企业走子Agent路径，由子Agent内部实现步骤1串行 + 步骤2/3/4并行。

> 本 Skill 输出仍受 `AgentRule §B 硬契约` 与 `§C 默认输出风格` 约束（不重复 / 不透传 JSON / 不编造数据）。
