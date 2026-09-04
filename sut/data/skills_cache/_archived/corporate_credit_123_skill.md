---
name: corporate_credit_123_skill
description: >
  公司信贷123智能体，针对企业客户进行综合全面深度分析，分4步顺序调用接口：
    步骤1 - 基础企业信息检索：获取企业概况、股权结构、财务概览、行业定位等基础数据。
    步骤2 - 企业综合分析（以步骤1结果为基础）：企业概况、区域特点、股东情况、负面舆情、核心优势、主要风险、行业机遇与挑战、控股股东支持能力及风险传导、主要业务机会、客户等级分类。
    步骤3 - 金融服务方案推荐（以步骤1结果为基础）：基于企业资料和银行制度文档，匹配金融产品方案，包含产品名称、制度依据、办理条件、融资要素、推荐理由、风险提示、尽职调查要点、政策依据、风控措施。
    步骤4 - 尽职调查清单（以步骤1结果为基础）：法律纠纷与司法风险、股权结构与关联交易、新业务拓展与产能消化、核心竞争力与研发转化、环保合规与绿色转型、劳动用工合规性。
  最终输出为步骤2、步骤3、步骤4的结果按顺序拼接。
  前置条件：调用本 Skill 前必须已确认 customer_name（客户名称）。
  补充数据（企业资料、财务报表、行业报告、银行制度文档等）由用户随问题一并提供，随 query 透传至后端。
  附件地址（企业资料、财务报表、行业报告、银行制度文档等文件地址）由用户随问题一并提供，随 ref_url 透传至后端。
  若 customer_name 缺失，禁止调用本 Skill。

  触发词：公司信贷、企业分析、金融服务方案、信贷方案、尽职调查、企业尽调、全面分析、信贷123。
  不要用于：个人信贷、理财产品推荐、保险推荐、资金转账、账户查询、信用评分。
---

# corporate_credit_123_skill

## 职责

依据联网搜索结果、企业资料、银行制度文档和附件内容，分4步调用 `call_versatile` 触发后端联网检索 + 大模型推理服务，对目标企业客户进行综合分析。步骤1的结果作为步骤2/3/4的输入，最终输出为步骤2/3/4的结果拼接。

## 工具白名单

- `call_versatile`

## 固定参数

本 Skill 所有 `call_versatile` 调用的固定参数：
- `query_intent`：`"公司信贷123"`
- `query_response_analysis_scripts`：`"python corporate_credit_123_skill/scripts/run_corporate_credit.py"`

## 输入槽位

从用户请求和上文对话中提取并保持一致：
- `customer_name`：客户名称（必填）。
- 补充数据：企业资料、财务报表、行业报告、银行制度文档等，直接透传至 `query` 的 `{supplementary_data}` 占位符中。
- 附件地址：企业资料、财务报表、行业报告、银行制度文档等，直接透传至 `query` 的 `{ref_url}` 占位符中。

## 执行流程

本 Skill 的核心是**步骤1 → 步骤2 → 步骤3 → 步骤4**的严格串行依赖关系。步骤1的结果需要LLM在thought中暂存，并在后续步骤的 `query` 中透传。步骤2/3/4**不得并行调用**，必须按顺序逐一调用 `call_versatile`，等待前一个步骤返回结果后，再发起下一个步骤的调用。

### 第一步：确认客户名称（仅思考，不调工具）

1. 从用户输入和上文对话中确认 `customer_name`。若缺失，禁止调用工具，按「终态回复模板 - 缺槽位」回复。
2. 提取用户提供的补充数据（企业资料、财务报表、行业报告、银行制度文档等）。

### 第二步：调用接口-基础企业信息检索（步骤1）

调用第一个接口获取企业基础数据，结果将在后续步骤中使用。

```
call_versatile(
  query="{customer_name}",
  query_intent="公司信贷123_A"
)
```

工具返回结构（步骤1）：
```json
{
  "status": "success",
  "baseInfo": ""
}
```

LLM 需将 `baseInfo` 中的内容暂存于thought中，用于拼装步骤2/3/4的 `query`。

### 第三步：调用接口-企业综合分析（步骤2）

以第二步的 `baseInfo` 为基础，调用第二个接口生成企业综合分析报告。

```
call_versatile(
  query="$${customer_name}$${supplementary_data_or_空}$${ref_url_空}$${baseInfo_text}$$",
  query_intent="公司信贷123_B"
)
```

工具返回结构（步骤2）：
```json
{
  "status": "success",
  "enterpriseAnalysis": ""
}
```

### 第四步：调用接口-金融服务方案推荐（步骤3）

以第二步的 `baseInfo` 为基础，调用第三个接口生成金融服务方案。

```
call_versatile(
  query="$${customer_name}$${supplementary_data_or_空}$${ref_url_空}$${baseInfo_text}$$",
  query_intent="公司信贷123_D"
)
```

工具返回结构（步骤3）：
```json
{
  "status": "success",
  "financialServicePlan": ""
}
```

### 第五步：调用接口-尽职调查清单（步骤4）

以第二步的 `baseInfo` 为基础，调用第四个接口生成尽职调查清单。

```
call_versatile(
  query="$${customer_name}$${supplementary_data_or_空}$${ref_url_空}$${baseInfo_text}$$",
  query_intent="公司信贷123_E"
)
```

工具返回结构（步骤4）：
```json
{
  "status": "success",
  "dueDiligenceChecklist": ""
}
```

### 第六步：输出结果

将步骤2、步骤3、步骤4的返回结果按顺序拼接输出。

## query 格式规范

| 步骤 | query_intent | query 格式 |
|------|---|---|
| 步骤1 | 公司信贷123_A | `{名称}` |
| 步骤2 | 公司信贷123_B | `$${名称}$${补充数据或空}$${附件路径或空}$${baseInfo}` |
| 步骤3 | 公司信贷123_D | `$${名称}$${补充数据或空}$${附件路径或空}$${baseInfo}` |
| 步骤4 | 公司信贷123_E | `$${名称}$${补充数据或空}$${附件路径或空}$${baseInfo}` |

`{baseInfo}` 替换为步骤1返回的 `baseInfo` 的文本；`supplementary_data` 缺省时写"空"；`ref_url` 缺省时写"空"。

## 强约束

- 本 Skill 仅辅助企业信贷分析判断和方案推荐，不替代客户经理的最终授信决策和风险审批。
- 必须先完成步骤1获取 `baseInfo`，再进行步骤2/3/4，不得调换顺序或跳过。
- 步骤2/3/4的 `query` 中必须包含步骤1返回的 `baseInfo`，不得编造基础数据。
- `enterpriseAnalysis` 各维度必须逐项呈现，某维度信息不足时标注"信息不足"，不得编造。
- `financialServicePlan` 中每个产品方案必须包含全部9个子字段，缺信息时标注"待确认"。
- `dueDiligenceChecklist` 按维度逐条输出具体核实方法和关注要点，不得笼统概括。
- 不得编造未在返回结果中出现的财务数据、法律文书或制度条文。
- query 必须严格按照格式规范填写，归一化脚本依赖步骤前缀解析。
- **一次 call_versatile 调用只分析一家公司。** 如需分析多家公司，必须在当前调用返回结果后再发起下一次调用，禁止同时发起多个并行调用。

## 终态回复模板

- 成功（status=success）：
  ```
  公司信贷综合全面分析报告 - {customer_name}

  第一部分：企业综合分析

  {enterpriseAnalysis}

  第二部分：金融服务方案推荐

  {financialServicePlan}

  第三部分：尽职调查清单

  {dueDiligenceChecklist}
  ```

- 分类失败：`公司信贷综合分析失败：{fail_cause}`
- 缺槽位：`缺少必要信息（客户名称），无法进行公司信贷综合分析。`

> 本 Skill 输出仍受 `AgentRule §B 硬契约` 与 `§C 默认输出风格` 约束（不重复 / 不透传 JSON / 不编造数据）。
