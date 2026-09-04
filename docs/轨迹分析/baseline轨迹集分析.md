# Baseline 全量批次轨迹集分析报告

- 批次：`runs/20260820_161933_weak_baseline_turbo_full/`（弱模型 qwen-turbo baseline）
- 分析脚本：`eval/analyze_traces.py`（机械统计）、`eval/classify_failures.py`（LLM 失败分类）、`eval/build_dataset.py`（数据集固化）
- 产物：`eval/trace_stats.jsonl`、`eval/task_groups.json`、`eval/failure_classification.jsonl`、`eval/dataset_v1.json`、`eval/dataset_v1_summary.json`

## 1. 总量与通过率

| 指标 | 数值 |
|---|---|
| 轨迹总数 | 345（115 个任务 × 3 轮） |
| 通过（reward=1.0） | 168 |
| 失败（reward=0.0） | 177 |
| 通过率 | **48.7%** |

## 2. 三组分布（按 task 聚合 3 轮结果）

| 分组 | 任务数 | 占比 |
|---|---|---|
| 稳定好（3 轮全过） | 32 | 27.8% |
| 稳定坏（3 轮全挂） | 35 | 30.4% |
| 分化（有挂有过） | 48 | 41.7% |

分化组占比最高（41.7%），说明弱模型行为方差大，同一任务多次采样结果不稳定，单次采样评测噪声大。

## 3. 失败类型分布（177 条 reward=0 轨迹，LLM 初判）

| 失败类型 | 条数 | 占失败比例 |
|---|---|---|
| 任务理解错误 | 65 | 36.7% |
| 参数错误 | 28 | 15.8% |
| 幻觉参数 | 27 | 15.3% |
| 工具误用 | 20 | 11.3% |
| 漏用户确认 | 18 | 10.2% |
| 其他 | 11 | 6.2% |
| 状态前置未查 | 5 | 2.8% |
| 漏身份认证 | 3 | 1.7% |
| 用户模拟器噪声 | 0 | 0% |

**「任务理解错误」为第一大类（36.7%）**：弱模型最常见的问题是误解用户意图（return/exchange 混淆、漏做多目标请求中的一项、对 pending 订单执行 delivered 订单的操作等）。「参数错误 + 幻觉参数」合计 31.1%，即约三分之一的失败涉及 id/参数层面的错误，其中幻觉参数（凭空捏造工具未返回过的 item_id/order_id）是弱模型的典型病症。政策流程类错误（漏用户确认、漏身份认证、状态前置未查）合计约 14.7%。

### 各类型典型 case

- **任务理解错误** — `runs/20260820_161933_weak_baseline_turbo_full/traces/trace_101_t1.json`：用户要求「return」登山靴（退货退款），agent 却调用 `exchange_delivered_order_items` 换成同款商品；还要求修改 pending 订单却被用 delivered 订单的 exchange 工具处理，意图完全理解反了。
- **参数错误** — `runs/20260820_161933_weak_baseline_turbo_full/traces/trace_109_t2.json`：agent 选了错误的订单 `#W2230795` 做退货（正确应为 `#W1679211`），用的是真实存在但张冠李戴的 order_id，且该订单非 delivered 状态连续报错。
- **幻觉参数** — `runs/20260820_161933_weak_baseline_turbo_full/traces/trace_102_t0.json`：修改手表订单时凭空捏造了工具从未返回过的 `new_item_id=9112290483`，工具直接报 "new item not found or available"。
- **工具误用** — `runs/20260820_161933_weak_baseline_turbo_full/traces/trace_101_t0.json`：对 pending 状态订单调用 `return_delivered_order_items`，报 "non-delivered order cannot be returned"，正确工具应为 `modify_pending_order_items`。
- **漏用户确认** — `runs/20260820_161933_weak_baseline_turbo_full/traces/trace_104_t1.json`：执行退货写操作前，未按政策列出详情并征得用户明确确认（yes）就直接执行。
- **状态前置未查** — `runs/20260820_161933_weak_baseline_turbo_full/traces/trace_15_t2.json`：未先 `get_product_details` 查目标尺码 item_id 就盲调 `modify_pending_order_items`，错误调用消耗了唯一修改机会导致后续无法补救。
- **漏身份认证** — `runs/20260820_161933_weak_baseline_turbo_full/traces/trace_17_t1.json`：未经 `find_user_id_by_name_zip` 验证身份就直接调 `modify_pending_order_address` 改地址。
- **其他** — `runs/20260820_161933_weak_baseline_turbo_full/traces/trace_28_t2.json`：把多个订单的商品错误地合并到一次 `return_delivered_order_items` 调用中，触发 "some item not found" 错误。

## 4. 全挂任务（稳定坏 35 个）甄别结论

35 个全挂任务共 105 条失败轨迹，LLM 判定 **user_simulator_fault = 0 条**，即：

- **用户模拟器噪声 / 疑似任务问题：0 个任务**
- **agent 能力问题：35 / 35 个任务（100%）**

全挂任务的失败类型分布：任务理解错误 45、参数错误 15、工具误用 13、幻觉参数 11、漏用户确认 10、其他 8、状态前置未查 3。与整体失败分布一致，以意图理解与参数层面错误为主。人工抽查了若干「其他」类及证据为空的条目（如 task 100/63/41），轨迹中 agent 均有明确可见的错误动作（错误工具、缺失的期望调用、多余写操作），reward=0 判定合理，未发现模拟器误判迹象。

结论：**本批次的失败几乎全部是 agent 能力问题**，数据集无需因模拟器噪声做清洗。

## 5. 数据集 v1 统计（`eval/dataset_v1.json`）

| 字段 | 统计 |
|---|---|
| 总条数 | 345 |
| include=true | **345** |
| include=false（剔除） | **0** |
| 剔除规则 | user_simulator_fault=true 者剔除；本批次无此类轨迹 |

每条记录含：trace 相对路径、task_id、trial、reward、group（稳定好/稳定坏/分化）、failure_type（成功为 null）、user_simulator_fault、include、note（LLM 证据摘要）。

## 6. 给后续 golden / coldstart 的建议输入规模

- **golden（正例库）**：32 个稳定好任务 × 3 轮 = **96 条高质量成功轨迹**可直接作为 golden 候选，建议每任务取 1 条最优（消息数适中、无多余调用）固化 32 条 golden，剩余 64 条作备用/增强。
- **coldstart（冷启动练习集）**：
  - 优先用「分化」组的失败轨迹做对照学习素材（48 任务，每任务同时有成功与失败样本，天然构成对比对）；
  - 「稳定坏」组 35 个任务全部保留为 coldstart 训练目标，覆盖全部 8 类失败模式；
  - 建议 coldstart 输入规模：**83 个任务（48 分化 + 35 稳定坏），对应 249 条轨迹**，其中失败轨迹 177 条全部可用于失败模式规避训练。
- 失败类型分布提示后续优化重点：任务理解（36.7%）与参数准确性（31.1%）合计近七成，应在技能注入 / prompt 层面优先解决 return vs exchange 辨析、pending vs delivered 订单工具选择、以及禁止捏造 id 的约束。

## 附：复现方式

```bash
agent/.venv/Scripts/python.exe eval/analyze_traces.py     # 任务一：机械统计
agent/.venv/Scripts/python.exe eval/classify_failures.py  # 任务二：LLM 分类（读 sut/.env，可断点续跑）
agent/.venv/Scripts/python.exe eval/build_dataset.py      # 任务三：固化 dataset_v1.json
```

注：`eval/failure_classification.jsonl` 为增量写入，重跑 `classify_failures.py` 会自动跳过已分类条目；如需重判请先删除该文件。
