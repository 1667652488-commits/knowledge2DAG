# -*- coding: utf-8 -*-
"""汇总 classification.jsonl -> report.md + 复核清单.csv"""
import csv
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = (ROOT / sys.argv[1]) if len(sys.argv) > 1 else sorted(
    ROOT.glob("runs/*_partial_review"))[-1]
recs = [json.loads(l) for l in (OUT_DIR / "classification.jsonl").open(encoding="utf-8")]

valid = [r for r in recs if r.get("llm_judge")]
n_err = len(recs) - len(valid)
verdicts = Counter(r["llm_judge"].get("verdict", "?") for r in valid)
issues = Counter(r["llm_judge"].get("issue_type", "?") for r in valid)
facts = Counter(r["llm_judge"].get("fact_check", "?") for r in valid)
total = len(valid)

def rate(rs):
    if not rs:
        return "n/a"
    tp = sum(1 for r in rs if r["llm_judge"].get("verdict") == "真发现")
    return f"{tp}/{len(rs)} = {tp/len(rs)*100:.1f}%"

hard_pass = [r for r in valid if r.get("reward") == 1.0]
hard_fail = [r for r in valid if r.get("reward") == 0.0]
hard_none = total - len(hard_pass) - len(hard_fail)

tp_pct = verdicts.get("真发现", 0) / total * 100 if total else 0
noise_pct = verdicts.get("噪声", 0) / total * 100 if total else 0
conclusion = (
    f"真发现占比 {tp_pct:.1f}%，噪声占比 {noise_pct:.1f}%。"
    + ("真发现占多数，说明 golden 对“部分通过”的扣分大多有据可依；建议改口径——将这部分真发现从“部分通过”中细分出来（或下调为不通过），并优先修复高频问题类型（"
       + "、".join(t for t, _ in issues.most_common(3)) + "），从源头减少与硬判分的分歧。"
       if tp_pct >= noise_pct else
       "噪声占多数，说明 golden 对“部分通过”的判定存在较多事实错误或吹毛求疵；建议再收 prompt——强化事实核对要求（扣分前必须引用轨迹证据）、放宽措辞/风格类苛求，以降低与硬判分的假分歧。")
)

lines = []
lines.append("# Golden“部分通过”归因分析报告\n")
lines.append(f"- 数据源：`runs/20261009_100203_golden/golden_output.jsonl`（result==部分通过）")
lines.append(f"- 输出目录：`{OUT_DIR.relative_to(ROOT)}`")
lines.append(f"- 总数：{len(recs)} 条（LLM 判定成功 {total} 条，失败 {n_err} 条）\n")
lines.append("## verdict 分布\n")
for v, c in verdicts.most_common():
    lines.append(f"- {v}：{c} 条（{c/total*100:.1f}%）")
lines.append("\n## issue_type 分布\n")
for t, c in issues.most_common():
    lines.append(f"- {t}：{c} 条（{c/total*100:.1f}%）")
lines.append("\n## fact_check 分布\n")
for f, c in facts.most_common():
    lines.append(f"- {f}：{c} 条（{c/total*100:.1f}%）")
lines.append(f"- 其中 fact_check=不成立：{facts.get('不成立', 0)} 条\n")
lines.append("## 按硬判分 reward 分层\n")
lines.append(f"- hard pass（reward=1.0）：{len(hard_pass)} 条，真发现率 {rate(hard_pass)}")
lines.append(f"- hard fail（reward=0.0）：{len(hard_fail)} 条，真发现率 {rate(hard_fail)}")
if hard_none:
    lines.append(f"- reward 缺失：{hard_none} 条")
lines.append("\n## 结论\n")
lines.append(conclusion)
report = "\n".join(lines) + "\n"
(OUT_DIR / "report.md").write_text(report, encoding="utf-8")

# 复核清单：按 issue_type 分层抽 20 条
random.seed(42)
by_type = {}
for r in valid:
    by_type.setdefault(r["llm_judge"].get("issue_type", "?"), []).append(r)
quota = {t: max(1, round(20 * len(rs) / total)) for t, rs in by_type.items()}
while sum(quota.values()) > 20:
    t = max(quota, key=lambda k: quota[k])
    quota[t] -= 1
while sum(quota.values()) < 20:
    t = max(by_type, key=lambda k: len(by_type[k]) - quota.get(k, 0))
    quota[t] += 1
sample = []
for t, q in quota.items():
    sample.extend(random.sample(by_type[t], min(q, len(by_type[t]))))
random.shuffle(sample)
with (OUT_DIR / "复核清单.csv").open("w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["id", "script_id", "trace_file", "reward", "issue_type",
                "golden_reason", "llm_verdict", "llm_fact_check", "llm_rationale"])
    for r in sample:
        j = r["llm_judge"]
        w.writerow([r["id"], r.get("script_id"), r.get("trace_file"), r.get("reward"),
                    j.get("issue_type"), r.get("reason"), j.get("verdict"),
                    j.get("fact_check"), j.get("rationale")])
print(report)
print(f"复核清单 {len(sample)} 条 -> {OUT_DIR / '复核清单.csv'}")
