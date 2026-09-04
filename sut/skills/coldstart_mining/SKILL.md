# coldstart_mining

skill-scoped cold-start 规则挖掘: trace + golden → if-then 规则(JSON + 自然语言)。
messages-only, LLM 走 common。

## 流程
1. 全局 phase2: 所有 trace → 全局检查点
2. skill-scoped phase2-6(按 badcase skill 分组, ≥2 条): → skill 特异 if-then 规则
3. 合并: 全局检查点 + skill 规则 → final_rules.json + 双版本 NL 规则

## 合并产出(merged/)
- `final_rules.json`: JSON if-then 规则(含 skill_attribution + supporting_trajectories)
- `final_rules_natural_language.txt`: badcase 评判者风格 NL 规则(给人读)
- `final_rules_diagnostic.txt`: LLM 改写的诊断规则块(塞入 3 维评估器 `{diagnostic_rules}` 槽位, 把通用评估器增强成 agent 专属)
- `evaluator_prompt_enhanced.md`: 诊断块填入 base prompt(`templates/evaluator_base_prompt.txt`)后的完整增强评估 prompt; 运行时再填 `{expected_section}/{skill_names}/{messages}`
  - 同一批 mined rules 两种渲染; LLM 失败/mock 时诊断块降级为机械罗列(不崩); 设 env `COLDSTART_NO_LLM_DIAG=1` 强制走机械罗列不调 LLM

phases/ 下:
- phase2.py 归纳正确链路 + 缺失检查点
- phase3.py 归纳 badcase 类别 + 检查点
- phase4.py LLM 特征提取(0/1/NA 判定, 配置驱动, 用 messages_trajectory_config.json)
- phase5.py 规则挖掘 + skill 归因
- phase6.py 规则语言化 + 排序 + expected_behavior

## 用法
```bash
python -m skills.coldstart_mining.run \
  --trace-dir data/traces --golden data/golden/golden_output.jsonl \
  --skills data/skills_flat --output runs/coldstart --min-traces 2 --batch-size 10
```

## 端点
LLM 走 common.llm_client(config.yaml `llm.*` 或 env `LLM_*`)。orchestrator 本身离线, 只 subprocess 调各 phase。
phase4 用 `phases/messages_trajectory_config.json` 声明 messages 格式(history_field=messages)。
