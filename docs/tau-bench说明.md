# τ-bench 说明

> 本项目选用的开源测试基准。仓库：`https://github.com/sierra-research/tau-bench`（Sierra 团队，论文 arXiv:2406.12045）。
> 本地位置：`agent/tau_bench/`（codeload 快照，非 git clone）。本文面向首次接触的读者。

## 1. 它是什么

一个测 **客服类工具调用 agent** 的 benchmark：agent 扮演客服，通过调用工具 API 帮"用户"完成任务（退货/换货/改订单等），全程必须遵守一本政策手册（policy）。考察的核心能力：**多轮对话中遵守规则 + 正确使用工具 + 应对真实用户的随意表达**。

两个领域：**retail**（零售，本项目用）和 **airline**（航空，未用）。

## 2. 四大组成

### 2.1 任务集（tasks）

- retail 三个 split：train / test / dev，本项目用 **test（115 个任务）**，定义在 `tau_bench/envs/retail/tasks_test.py`；
- 每个任务 = `{user_id, instruction, actions, outputs}`：
  - `instruction`：用户人设 + 目标（英文，写得像真人随口提的需求，有的还带分支偏好——"如果没有 clicky 轴的键盘就只换恒温器"）；
  - `actions`：**期望工具调用序列**（判分依据，见 §3）；
  - `outputs`：问答型任务的期望答复内容（可空）。

### 2.2 政策手册（wiki）

- `tau_bench/envs/retail/wiki.md`（81 行英文），agent 的唯一规则来源，进 system prompt；
- 关键条款：对话开头必须认证身份（邮箱或姓名+邮编）、写操作前必须列明细并获得用户明确确认（yes）、一次只能一个 tool call、取消/退货/换货各自的状态前置与退款规则、不得编造工具没返回的信息等；
- 对我们的价值：**它是 GU（全局理解）和"正确 SOP"的天然真值**。

### 2.3 工具 API（15 个，本地模拟）

- `tau_bench/envs/retail/tools/*.py`，纯本地 Python 实现，操作内存中的 JSON 数据库（`envs/retail/data/` 下的 orders/users/products），**不连任何外部服务**；
- 查询类：`find_user_id_by_email` / `find_user_id_by_name_zip` / `get_user_details` / `get_order_details` / `get_product_details` / `list_all_product_types` / `calculate`；
- 写操作类：`cancel_pending_order` / `modify_pending_order_address` / `modify_pending_order_items` / `modify_pending_order_payment` / `modify_user_address` / `return_delivered_order_items` / `exchange_delivered_order_items`；
- 特殊：`transfer_to_human_agents`（转人工）、`think`（内部思考，不计动作）；
- 工具会校验规则并报错（如对非 delivered 订单退货返回 Error），但**不校验"是否认证过""是否用户确认过"这类流程规则**——违反这些照样执行成功，只能靠终态判分或事后审查发现；
- 我们把 15 个工具各包了一份中文 skill 文档：`data/skills_flat/*.md`（供 golden/coldstart 做 skill 归因）。

### 2.4 用户模拟器

- **用户不是剧本，是 LLM 实时扮演**（`UserStrategy.llm`）：instruction 作为隐藏人设发给它，它看着对话历史逐轮生成用户回复，需求满足或谈崩时输出 `###STOP###` 结束；
- 同一任务每次跑措辞都不同（这是 num_trials 采样的价值）；
- 本项目所有批次的用户模拟器固定 **GLM5.1**，不作为实验变量。

## 3. 判分机制（硬锚点）

全程程序化，无 LLM 裁判：

- **r_actions（数据库终态比对）**：环境把 expected actions 在影子数据库上执行一遍得到"正确终态"哈希（日志里的 `gt_data_hash`），与 agent 实际跑完的数据库终态哈希比对——不一致即失败。说漂亮话没用，账没改对就挂；
- **r_outputs（输出内容核对）**：问答型任务检查 agent 最终回复是否包含期望答案；
- 最终 `reward` ∈ {0.0, 1.0}，存于结果 JSON 每条的 `reward` 字段，adapter 抽取为批次的 `tau_reward.json`。

## 4. agent harness

- 官方自带 baseline agent：`tool-calling`（函数调用循环，本项目用）/ `act` / `react` / `few-shot`；
- 形态：**单 agent 串行**——system prompt（政策手册全文）+ 15 个工具 schema 一次给全，循环"LLM 决策 → 调一个工具 → 拿结果"；
- 与我们生产形态的差异：**无多 agent 编排、无按 skill 动态加载**，一期接受此差异（记录进测试报告"与生产差异"节）。

## 5. 本项目的使用方式

```bash
# 跑批（agent/.venv 环境，key 从 sut/.env 自动映射）
agent/.venv/Scripts/python agent/run_batch.py --tier weak --suffix baseline_turbo_full \
    --model openai/qwen-turbo --user-model openai/glm-5.1 \
    --start 0 --end 115 --num-trials 3 --temperature 0.6 --max-concurrency 8
```

- 批次产物：`runs/<ts>_<tier>/{raw/, traces/, tau_reward.json, batch_meta.json}`；
- 已知坑（已修）：模型名带 `/`（如 `openai/glm-5.1`）时 τ-bench 拼 checkpoint 文件名报错——本地 patch 了 `tau_bench/run.py` 的 ckpt 文件名（`user_model.split('/')[-1]`）；
- 本地依赖环境：`agent/.venv`（Python 3.11，清华镜像安装）。

## 6. 已知局限（评估解读时需注意）

- **用户模拟器噪声**：少数 reward=0 可能是模拟器没表达清楚需求诱导的，归因分析时要甄别"agent 的锅"还是"用户的锅"；
- **判分只看终态**：流程违规但终态碰巧正确（如漏了用户确认但操作本身对）会判过——这类"结果对但过程错"的 case 恰恰是 golden 软标要抓的，也是对拍①最值得分析的分歧类型；
- 任务量级小（115），统计结论的置信度有限，靠 num_trials 放量缓解。
