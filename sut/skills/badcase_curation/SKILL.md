# badcase_curation

badcase/goodcase 精选。依赖 **golden + cold-start**, 不依赖 checker。

## 数据流(无环)
```
golden 全量判 → cold-start phase2-6 跑 badcase池 → final_rules.json
    → curation 读 golden(result+scenario) + final_rules(badcase错误类型) + trace(复杂度)
    → 分桶选取 → chosen/badcase + chosen/good + README
```

## 选取规则
- **badcase 池**(golden result ∈ 失败/部分通过): 按 cold-start `error_category` 分桶, per-桶上限, 复杂度(tool_call数)降序, LLM 核验(防假阳性)
- **goodcase 池**(golden result = 通过): 按 `scenario` 分桶, per-桶上限, 复杂度降序, 不核验
- 每桶上限: 显式 > `_default`(未列出/全新桶走它) > `0=跳过`
- badcase↔goodcase 同 scenario 配对照(README 标注, 有则配无则单列)
- "只有 goodcase 无 badcase"的场景: goodcase 独立选, 不强制配 badcase

## badcase 类型怎么来
cold-start `final_rules.json` 每条 rule 有 `error_category` + `supporting_trajectories`(conversation_id)。curation 用 trace.conversation_id 反查命中哪条 rule → 该 rule 的 error_category 即 badcase 类型。命中不到 → "未归类"桶。

## scenario 怎么来
golden 输出的 `scenario` 字段(剧本下=script.category, 回流下=LLM 打标签)。golden 无此字段时 curation 回退到 trace.script.category。

## 用法
```bash
python run.py curation
python -m skills.badcase_curation.run --golden data/golden/golden_output.jsonl --rules data/runs/coldstart/merged/final_rules.json --trace-dir data/traces
```

## config
```yaml
selection:
  badcase: { _default: 2, "CAT005-...": 5 }   # 0=跳过该类
  goodcase: { _default: 1, "数据透传-baseInfo": 3 }
  verify_badcase: true   # goodcase 永不核验
```
