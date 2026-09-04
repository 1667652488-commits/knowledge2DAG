# fault_localization — 失败 case 根因定位到 skill / AgentRule

> 通用 skill, 不绑定具体 agent/业务。skill 清单、AgentRule 职责、rule 的 skill 归属均从运行时数据读取, 迁移到其他 agent 场景可直接复用。

## 定位什么
给定 golden(已带 expected_behavior + result), 对**失败/部分通过**的 case, 反推"根因该改哪个 skill"。归属两类:
- **某具体 skill**: 该 skill 内部逻辑/参数/触发不对。
- **AgentRule**(根级全局规则): 路由错、该调没调、越界没拒答、编排流程错、能力边界没识别。

**特殊规则**: 归到 AgentRule 时, 额外给出"除 AgentRule 外首个应改的 skill"(被错误调用或本应调用的那个) —— 编排层光改往往不够, 要连带改底层 skill 才闭环。

## 怎么定位(以 coldstart rule 反查为主, LLM 兜底)
1. 加载 coldstart `final_rules.json`(兼容两种结构), 按 `supporting_trajectories` 建 `conversation_id → [rules]` 反查表。
2. 每条失败 case 用 `golden.id`(= conversation_id)反查:
   - 命中 rule 且有 `skill_attribution.top3` → 用 top1 定位; top1 为 AgentRule 则附加 top3 首个非 AgentRule skill。**省 LLM、与 coldstart 一致。**
   - 命中 rule 但无 skill_attribution(scoped phase6 输出) → 退用 rule 所属 skill; 仍不明则转 LLM。
   - 未命中(未归类) → LLM 兜底: 喂 trace + expected_behavior + reason + skill 清单 + AgentRule 职责 → 判归属。

## 输入 / 输出
- 输入: `golden_output.jsonl` + `final_rules.json` + traces 目录 + skills_flat + AgentRule.md(自动读 bundle 根)
- 输出(**不改原 golden**):
  - `golden_localized.jsonl`: 原 golden 同形 + `localization` 字段(attributed_to / is_agentrule / secondary_skill / matched_rules / source / problematic_rule); 通过的 case `localization=null`
  - `localization_report.md`: 汇总表(每 skill/AgentRule 命中数+占比+典型 case; AgentRule 行带"最常连带 skill")

## 运行
```bash
python run.py localize --golden data/golden/golden_output.jsonl --rules data/runs/coldstart/merged/final_rules.json --trace-dir data/traces
# 不想调 LLM(未归类 case 直接标"未归类"):
python run.py localize --no-llm
```
依赖关系: 跑在 `coldstart` 之后(吃它的 final_rules.json)。trace 用于 LLM 兜底, 没有也能跑(降级为只用 golden 字段)。
