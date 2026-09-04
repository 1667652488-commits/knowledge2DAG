#!/usr/bin/env python3
"""
quality_stats.py — golden expected_behavior 与自然语言规则的静态质量指标(不调 LLM)
"""
import json, re, sys, statistics, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common.config import get_config
if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")

_cfg = get_config()
GOLDEN = str(_cfg.paths.golden / "golden_output.jsonl")
RULES = str(_cfg.paths.runs / "coldstart" / "merged" / "final_rules.json")

def p(*a): print(*a)

# ============ golden expected_behavior 质量 ============
p("=" * 70)
p("【一】golden expected_behavior 质量指标")
p("=" * 70)
entries = [json.loads(l) for l in open(GOLDEN, encoding="utf-8")]
p(f"总数: {len(entries)}")

# 字段完整度
fields = ["id", "script_id", "inputs", "expected_behavior", "result", "reason"]
for f in fields:
    miss = sum(1 for e in entries if not e.get(f))
    p(f"  字段 {f}: 完整 {len(entries)-miss}/{len(entries)} ({(len(entries)-miss)/len(entries):.0%})")

# result 分布
from collections import Counter
res = Counter(e["result"] for e in entries)
p(f"  result 分布: {dict(res)}")

# expected_behavior 格式合规: "在...情况下,应该/应..." 模式
pat_scenario = re.compile(r"在.{0,40}?(情况|场景|时|下)")
pat_should = re.compile(r"(应该|不应|应确保|应当|不应当|需|需要|要确保)")
fmt_ok = 0
len_eb = []
for e in entries:
    eb = e.get("expected_behavior", "")
    len_eb.append(len(eb))
    if pat_scenario.search(eb) and pat_should.search(eb):
        fmt_ok += 1
p(f"  expected_behavior 格式合规(含场景+应该/不应): {fmt_ok}/{len(entries)} ({fmt_ok/len(entries):.0%})")
p(f"  expected_behavior 长度: min={min(len_eb)} max={max(len_eb)} 均值={statistics.mean(len_eb):.0f} 中位={statistics.median(len_eb):.0f}")

# inputs 覆盖
empty_inputs = sum(1 for e in entries if not e.get("inputs"))
p(f"  inputs 非空: {len(entries)-empty_inputs}/{len(entries)}")

# reason 与 result 一致性: 失败/部分通过 应有非空 reason
need_reason = [e for e in entries if e["result"] in ("失败", "部分通过")]
has_reason = sum(1 for e in need_reason if e.get("reason", "").strip())
p(f"  失败/部分通过 条目 reason 非空: {has_reason}/{len(need_reason)}")

# script_id 唯一性
sids = [e["script_id"] for e in entries]
p(f"  script_id 唯一: {len(set(sids))}/{len(sids)}")

# ============ 自然语言规则 质量 ============
p("")
p("=" * 70)
p("【二】自然语言规则 (final_rules.json) 质量指标")
p("=" * 70)
d = json.load(open(RULES, encoding="utf-8"))
rules = d.get("rules", d if isinstance(d, list) else [])
p(f"规则总数: {len(rules)}")
rejected = sum(1 for r in rules if r.get("rejected"))
p(f"  rejected(被拒): {rejected}")

# score 分布
for key in ["score_rationality", "score_importance", "score_frequency", "score_total", "confidence"]:
    vals = [r.get(key) for r in rules if r.get(key) is not None]
    if vals:
        p(f"  {key}: n={len(vals)} min={min(vals):.2f} max={max(vals):.2f} 均值={statistics.mean(vals):.2f} 中位={statistics.median(vals):.2f}")

# if_conditions 数量
ic = [len(r.get("if_conditions", [])) for r in rules]
p(f"  if_conditions 数/规则: min={min(ic)} max={max(ic)} 均值={statistics.mean(ic):.1f}")
# 每条 if_condition 是否含 feature_description + judgment_criteria
full_cond = 0
total_cond = 0
for r in rules:
    for c in r.get("if_conditions", []):
        total_cond += 1
        if c.get("feature_description") and c.get("judgment_criteria"):
            full_cond += 1
p(f"  if_condition 字段完整(feature_description+judgment_criteria): {full_cond}/{total_cond} ({full_cond/total_cond:.0%})" if total_cond else "  无 if_condition")

# judgment_criteria 含 True/False 判定分支
has_tf = 0
for r in rules:
    crit = " ".join(c.get("judgment_criteria", "") for c in r.get("if_conditions", []))
    if "判定为True" in crit and "判定为False" in crit:
        has_tf += 1
p(f"  judgment_criteria 含 True/False 双分支: {has_tf}/{len(rules)} ({has_tf/len(rules):.0%})")

# expected_behavior (规则级) + error_reason 完整
eb_full = sum(1 for r in rules if r.get("expected_behavior", "").strip())
er_full = sum(1 for r in rules if r.get("error_reason", "").strip())
p(f"  规则级 expected_behavior 非空: {eb_full}/{len(rules)} ({eb_full/len(rules):.0%})")
p(f"  error_reason 非空: {er_full}/{len(rules)} ({er_full/len(rules):.0%})")

# few_shots 覆盖
neg = sum(1 for r in rules if r.get("few_shots", {}).get("negative_examples"))
pos = sum(1 for r in rules if r.get("few_shots", {}).get("positive_examples"))
st = sum(1 for r in rules if r.get("supporting_trajectories"))
p(f"  few_shots.negative_examples 覆盖: {neg}/{len(rules)} ({neg/len(rules):.0%})")
p(f"  few_shots.positive_examples 覆盖: {pos}/{len(rules)} ({pos/len(rules):.0%})")
p(f"  supporting_trajectories 覆盖: {st}/{len(rules)} ({st/len(rules):.0%})")

# skill_attribution top1 覆盖
attr = sum(1 for r in rules if r.get("skill_attribution", {}).get("top3"))
p(f"  skill_attribution.top3 非空: {attr}/{len(rules)} ({attr/len(rules):.0%})")

# error_category 分布
ec = Counter(r.get("error_category", "")[:20] for r in rules)
p(f"  error_category 分布:")
for k, v in ec.most_common():
    p(f"    {v:2d}  {k}")

# 自然语言版文件
import os
nl = str(_cfg.paths.runs / "coldstart" / "merged" / "final_rules_natural_language.txt")
if os.path.exists(nl):
    txt = open(nl, encoding="utf-8").read()
    p(f"  自然语言版文件: {nl}  {len(txt)} 字符, {txt.count(chr(10))+1} 行")
