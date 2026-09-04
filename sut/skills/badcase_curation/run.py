#!/usr/bin/env python3
"""
badcase_curation — badcase/goodcase 精选(依赖 golden + cold-start, 不依赖 checker)。

数据流(无环):
  golden 全量判(全跑批) → cold-start phase2-6 跑 badcase池 → final_rules.json
        → curation 读取 golden(result+scenario) + final_rules(badcase类型) + trace(复杂度)
        → 按 错误类型(badcase)/场景(goodcase) 分桶 + per-桶上限 + 复杂度降序选取
        → chosen/badcase + chosen/good + README

选取规则:
  badcase 池(golden result ∈ 失败/部分通过): 按 cold-start error_category 分桶, per-桶上限, 复杂度降序, LLM 核验
  goodcase 池(golden result = 通过): 按 scenario 分桶, per-桶上限, 复杂度降序, 不核验
  每桶上限: 显式 > _default(全新桶走它) > 0=跳过
  badcase↔goodcase 同 scenario 配对照(README 标注, 有则配无则单列)

用法:
    python run.py curation   # 经 bundle 根 run.py
    python -m skills.badcase_curation.run --golden data/golden/golden_output.jsonl --rules data/runs/coldstart/merged/final_rules.json --trace-dir data/traces
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

_BUNDLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BUNDLE))

from common.llm_client import call_llm  # noqa: E402
from common.json_utils import call_llm_json  # noqa: E402
from common import trace_io, state  # noqa: E402
from common.config import get_config  # noqa: E402

SKILL_DIR = Path(__file__).parent


def load_prompt(name: str, **variables: Any) -> str:
    text = (SKILL_DIR / "prompts" / name).read_text(encoding="utf-8")
    for k, v in variables.items():
        text = text.replace(f"{{{{{k}}}}}", str(v))
    return text


def parse_args():
    ap = argparse.ArgumentParser(description="badcase 精选器")
    ap.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--golden", default=None)
    ap.add_argument("--trace-dir", default=None)
    ap.add_argument("--rules", default=None)
    ap.add_argument("--output-dir", default=None)
    return ap.parse_args()


# ── 数据加载 ────────────────────────────────────────────────

def load_golden(path: str) -> Dict[str, dict]:
    """golden_output.jsonl → {script_id: {result, scenario, reason, expected_behavior, inputs}}"""
    m = {}
    if not path or not Path(path).exists():
        return m
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        m[r.get("script_id", "")] = {
            "result": r.get("result", ""),
            "scenario": r.get("scenario", ""),
            "reason": r.get("reason", ""),
            "expected_behavior": r.get("expected_behavior", ""),
            "inputs": r.get("inputs", []),
        }
    return m


def load_traces(trace_dir: str) -> Dict[str, dict]:
    """trace_*.json → {script_id: {conversation_id, complexity, script_category, file}}"""
    m = {}
    for t in trace_io.load_traces(trace_dir):
        sid = t.get("script_id") or (t.get("script", {}) or {}).get("id", "")
        if not sid:
            continue
        msgs = trace_io.get_messages(t)
        complexity = len(trace_io.get_all_tool_calls(msgs))
        script = t.get("script") or {}
        cat = script.get("category", "") if isinstance(script, dict) else ""
        m[sid] = {
            "conversation_id": t.get("conversation_id", ""),
            "complexity": complexity,
            "script_category": cat,
            "file": str(Path(trace_dir) / f"trace_{sid}.json"),
        }
    return m


def load_rules(rules_path: str) -> List[dict]:
    """final_rules.json → [{error_category, supporting_cids(set), score}]。

    兼容三种结构:
    1) {rules: [...]}                  (V3 种子, 扁平)
    2) [...]                           (顶层 list)
    3) {scoped_rules: [{rules:[...]}]} (bundle merge_rules, 按 skill 嵌套)
    """
    if not rules_path or not Path(rules_path).exists():
        return []
    d = json.loads(Path(rules_path).read_text(encoding="utf-8"))
    if isinstance(d, list):
        rules = d
    elif isinstance(d, dict):
        rules = d.get("rules")
        if not rules and d.get("scoped_rules"):
            # bundle merge_rules 结构: scoped_rules[*].rules 是 phase6 的 {meta, rules:[...], rejected_rules} dict
            rules = []
            for sr in d.get("scoped_rules", []) or []:
                sr_rules = (sr or {}).get("rules", [])
                if isinstance(sr_rules, dict):   # phase6 输出是 dict, 再解一层
                    sr_rules = sr_rules.get("rules", []) or sr_rules.get("rejected_rules", [])
                if isinstance(sr_rules, list):
                    rules.extend(sr_rules)
    else:
        rules = []
    rules = rules or []
    out = []
    for r in rules:
        cids = set(r.get("supporting_trajectories") or [])
        for ne in (r.get("few_shots", {}) or {}).get("negative_examples", []) or []:
            if ne.get("conversation_id"):
                cids.add(ne["conversation_id"])
        out.append({
            "error_category": r.get("error_category", r.get("then_category", "")),
            "supporting_cids": cids,
            "score": float(r.get("score_total", r.get("confidence", 0.5))),
        })
    return out


def build_type_map(traces: Dict[str, dict], rules: List[dict]) -> Dict[str, str]:
    """trace script_id → badcase 错误类型(命中某 rule 的 supporting cid, 取最高分 rule); 未命中=未归类"""
    cid_to_type = {}
    for r in rules:
        for cid in r["supporting_cids"]:
            # 多 rule 命中取高分
            if cid not in cid_to_type or r["score"] > cid_to_type[cid][1]:
                cid_to_type[cid] = (r["error_category"], r["score"])
    out = {}
    for sid, t in traces.items():
        pair = cid_to_type.get(t["conversation_id"])
        out[sid] = pair[0] if pair else "未归类"
    return out


# ── 选取 ────────────────────────────────────────────────────

def _cap_for(key: str, caps: dict) -> int:
    return int(caps.get(key, caps.get("_default", 0)))


def select_pool(cases: List[dict], key_fn, caps: dict, verify_fn=None) -> List[dict]:
    """按 key 分桶, per-桶上限(0=跳过), 复杂度降序取前N, 可选核验。"""
    groups = defaultdict(list)
    for c in cases:
        groups[key_fn(c)].append(c)
    selected = []
    for key, items in groups.items():
        cap = _cap_for(key, caps)
        if cap == 0:
            continue
        items.sort(key=lambda c: c.get("complexity", 0), reverse=True)
        picked = items[:cap]
        if verify_fn:
            picked = [c for c in picked if verify_fn(c)]
        for c in picked:
            c["bucket"] = key
        selected.extend(picked)
    return selected


def verify_badcase(case: dict) -> bool:
    """LLM 核验单条 badcase 是否为真(防假阳性)。mock 返回 true。"""
    trace_path = case.get("file")
    if not trace_path or not Path(trace_path).exists():
        return True
    trace = trace_io.load_trace(trace_path)
    payload = json.dumps({
        "script_id": case["script_id"],
        "result": case["result"],
        "expected_behavior": case.get("expected_behavior", ""),
        "reason": case.get("reason", ""),
        "messages": trace.get("messages", [])[:20],
    }, ensure_ascii=False, indent=2)
    try:
        prompt = load_prompt("verify_badcase.txt", case_content=payload)
        v = call_llm_json(call_llm, [
            {"role": "system", "content": "你是一名严格的 badcase 审核员。只输出 JSON。"},
            {"role": "user", "content": prompt},
        ])
        verdict = v.get("verdict", "") if isinstance(v, dict) else ""
        # 拿不到 verdict(mock/解析失败)时保留, 不在技术故障时丢 case; 明确 false_positive 才丢
        if not verdict:
            return True
        return verdict == "true_badcase"
    except Exception:
        return True  # 解析失败不丢 case


# ── 拷贝 + README ────────────────────────────────────────────

def copy_selected(selected: List[dict], out_dir: Path, sub: str) -> None:
    d = out_dir / sub
    d.mkdir(parents=True, exist_ok=True)
    for c in selected:
        src = Path(c.get("file", ""))
        if src.exists():
            shutil.copy2(src, d / src.name)


def generate_readme(badcases: List[dict], goodcases: List[dict],
                    scenario_map: dict, source: str, out_dir: Path) -> str:
    """badcase 按 error_category 桶 + goodcase 按 scenario 桶 + 同 scenario 对照。"""
    lines = [f"# Badcase 精选报告", "", f"> 来源: {source}", ""]

    # badcase 按错误类型
    lines.append("## Badcase(按错误类型 / cold-start phase3)")
    bad_groups = defaultdict(list)
    for c in badcases:
        bad_groups[c.get("bucket", "未归类")].append(c)
    if not bad_groups:
        lines.append("- (无)")
    for cat, items in sorted(bad_groups.items()):
        lines.append(f"\n### {cat} ({len(items)} 条)")
        for c in items:
            lines.append(f"- `{c['script_id']}` (复杂度 {c['complexity']}) | {c.get('reason','')[:80]}")

    # goodcase 按场景
    lines.append("\n## Goodcase 对照(按 scenario)")
    good_groups = defaultdict(list)
    for c in goodcases:
        good_groups[c.get("bucket", "未归类")].append(c)
    if not good_groups:
        lines.append("- (无)")
    for sc, items in sorted(good_groups.items()):
        lines.append(f"\n### {sc} ({len(items)} 条)")
        for c in items:
            lines.append(f"- `{c['script_id']}` (复杂度 {c['complexity']})")

    # 同 scenario 对照(badcase 的 scenario ↔ goodcase 的 scenario)
    lines.append("\n## 同场景对照(badcase ↔ goodcase)")
    bad_by_sc = defaultdict(list)
    for c in badcases:
        bad_by_sc[scenario_map.get(c["script_id"], "未归类")].append(c["script_id"])
    good_by_sc = defaultdict(list)
    for c in goodcases:
        good_by_sc[c.get("bucket", "未归类")].append(c["script_id"])
    paired = 0
    for sc in sorted(set(bad_by_sc) | set(good_by_sc)):
        b = bad_by_sc.get(sc, [])
        g = good_by_sc.get(sc, [])
        if b or g:
            lines.append(f"- **{sc}**: bad={b} good={g}")
            if b and g:
                paired += 1
    lines.append(f"\n*配对照的场景数: {paired}*")

    md = "\n".join(lines)
    return md


# ── main ────────────────────────────────────────────────────

def main():
    args = parse_args()
    cfg = get_config()
    import yaml
    sk = yaml.safe_load((SKILL_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
    sp = sk.get("paths", {})
    sel = sk.get("selection", {})

    golden_path = args.golden or sp.get("golden", str(cfg.paths.golden / "golden_output.jsonl"))
    trace_dir = args.trace_dir or sp.get("trace_dir", str(cfg.paths.traces))
    rules_path = args.rules or sp.get("rules", str(cfg.paths.runs / "coldstart" / "merged" / "final_rules.json"))
    out_dir = Path(args.output_dir or sp.get("output_chosen_dir", str(cfg.paths.chosen)))
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[加载] golden={golden_path}")
    golden = load_golden(golden_path)
    print(f"  golden: {len(golden)} 条")
    traces = load_traces(trace_dir)
    print(f"  traces: {len(traces)} 条")
    rules = load_rules(rules_path)
    print(f"  rules: {len(rules)} 条(cold-start error_category)")
    type_map = build_type_map(traces, rules)
    # scenario: golden.scenario 优先, 否则 trace.script_category, 否则"未归类"
    scenario_map = {sid: (g["scenario"] or traces.get(sid, {}).get("script_category", "") or "未归类")
                    for sid, g in golden.items()}

    # 组装 case 列表
    all_cases = []
    for sid, g in golden.items():
        t = traces.get(sid, {})
        if g["result"] not in ("失败", "部分通过", "通过"):
            continue  # NA 跳过
        all_cases.append({
            "script_id": sid,
            "result": g["result"],
            "scenario": scenario_map.get(sid, "未归类"),
            "badcase_type": type_map.get(sid, "未归类"),
            "complexity": t.get("complexity", 0),
            "file": t.get("file", ""),
            "expected_behavior": g["expected_behavior"],
            "reason": g["reason"],
        })

    badcase_pool = [c for c in all_cases if c["result"] in ("失败", "部分通过")]
    goodcase_pool = [c for c in all_cases if c["result"] == "通过"]
    print(f"[分池] badcase池={len(badcase_pool)} goodcase池={len(goodcase_pool)}")

    bad_caps = sel.get("badcase", {"_default": 2})
    good_caps = sel.get("goodcase", {"_default": 1})
    verify_on = bool(sel.get("verify_badcase", True))

    verify_fn = verify_badcase if verify_on else None
    badcases = select_pool(badcase_pool, key_fn=lambda c: c["badcase_type"],
                           caps=bad_caps, verify_fn=verify_fn)
    goodcases = select_pool(goodcase_pool, key_fn=lambda c: c["scenario"],
                            caps=good_caps, verify_fn=None)
    print(f"[选取] badcase={len(badcases)} goodcase={len(goodcases)}")

    state.save("badcase_curation", "selected", {"badcase": badcases, "goodcase": goodcases}, args.run_id)

    copy_selected(badcases, out_dir, "badcase")
    copy_selected(goodcases, out_dir, "good")
    readme = generate_readme(badcases, goodcases, scenario_map, f"{golden_path}+{rules_path}", out_dir)
    (out_dir / sel.get("readme_name", "README.md")).write_text(readme, encoding="utf-8")

    print(f"\n[完成] {out_dir} | badcase={len(badcases)} goodcase={len(goodcases)}")
    print(f"  {out_dir}/badcase/  {out_dir}/good/  {out_dir}/README.md")


if __name__ == "__main__":
    main()
