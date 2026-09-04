#!/usr/bin/env python3
"""
fault_localization —— 失败 case 根因定位到 skill / AgentRule。

输入:
  - golden(jsonl, 取 result ∈ 失败/部分通过; golden.id == trace.conversation_id)
  - coldstart final_rules.json(rule 带 skill_attribution.top3 + supporting_trajectories)
  - traces 目录(按 conversation_id 关联, 供 LLM 兜底阅读)

定位逻辑(以 coldstart rule 反查为主, LLM 兜底):
  1. 加载 final_rules.json(兼容两种结构: 扁平 {rules:[...]} / bundle {scoped_rules:[{skill, rules:{meta,rules:[...]}}]})
     按 supporting_trajectories 建 conversation_id -> [rules] 反查表。
  2. 每条失败 case(golden.id):
     - 命中 rule 且 rule 有 skill_attribution: 用 top3 定位。
       top1.skill_name 为空或 AgentRule -> 归 AgentRule; 附加"除 AgentRule 外首个应改 skill" = top3 第一个非空非 AgentRule 项。
       否则 -> 归该 skill。
     - 命中 rule 但无 skill_attribution(如 scoped phase6 输出): 归 rule 所属 skill; AgentRule 性不明 -> 转 LLM 兜底定附加 skill。
     - 未命中任何 rule(未归类): LLM 兜底(喂 trace + expected_behavior + reason + skills 清单 + AgentRule 职责)。

输出(不改原 golden):
  - golden_localized.jsonl  : 原 golden 同形 + localization 字段(通过的 case localization=null)
  - localization_report.md  : 汇总(每 skill/AgentRule 频次+占比+典型 case+修改建议; AgentRule 行带最常连带 skill)

用法:
    python run.py localize --golden data/golden/golden_output.jsonl --rules data/runs/coldstart/merged/final_rules.json --trace-dir data/traces
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

_BUNDLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BUNDLE))

from common.llm_client import call_llm  # noqa: E402
from common.json_utils import is_llm_error, call_llm_json  # noqa: E402
from common.config import get_config  # noqa: E402
from common import trace_io  # noqa: E402
from common import state  # noqa: E402

SKILL_DIR = Path(__file__).parent

AGENTRULE_TOKENS = {"", "agentrule", "agent_rule", "global", "全局", "none", "null", "未知"}


def load_prompt(name: str, **variables: Any) -> str:
    text = (SKILL_DIR / "prompts" / name).read_text(encoding="utf-8")
    for k, v in variables.items():
        text = text.replace(f"{{{{{k}}}}}", str(v))
    return text


# ── rule 加载(兼容两种结构) ───────────────────────────────────

def _normalize_rule(r: dict, scoped_skill: Optional[str] = None) -> Optional[dict]:
    """统一 rule 字段; 保留 skill_attribution / supporting_trajectories / 所属 skill。"""
    if not isinstance(r, dict):
        return None
    nr = {
        "id": r.get("id", ""),
        "error_category": r.get("error_category", r.get("then_category", "")),
        "error_reason": r.get("error_reason", ""),
        "skill_attribution": r.get("skill_attribution"),
        "supporting_trajectories": r.get("supporting_trajectories", []) or [],
        "scoped_skill": scoped_skill or r.get("_skill_group") or r.get("skill", ""),
        "confidence": r.get("confidence"),
    }
    return nr


def load_rules(path: Path) -> List[dict]:
    """加载 final_rules.json, 兼容:
       A) 扁平 {rules:[...]}  (V3 seed)
       B) bundle {scoped_rules:[{skill, rules:{meta,rules:[...]}}]}  (coldstart_mining phase6)
       C) 顶层列表 [...]
    """
    if not path.exists():
        print(f"[警告] rules 不存在: {path}")
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: List[dict] = []
    if isinstance(data, list):
        for r in data:
            nr = _normalize_rule(r)
            if nr:
                out.append(nr)
        return out
    if not isinstance(data, dict):
        return out

    # A) 扁平
    flat = data.get("rules")
    if isinstance(flat, list):
        for r in flat:
            nr = _normalize_rule(r)
            if nr:
                out.append(nr)
    # B) scoped
    for sr in data.get("scoped_rules", []) or []:
        if not isinstance(sr, dict):
            continue
        sk = sr.get("skill")
        sr_rules = sr.get("rules", [])
        if isinstance(sr_rules, dict):  # {meta, rules:[...], rejected_rules}
            sr_rules = sr_rules.get("rules", []) or sr_rules.get("rejected_rules", [])
        if isinstance(sr_rules, list):
            for r in sr_rules:
                nr = _normalize_rule(r, scoped_skill=sk)
                if nr:
                    out.append(nr)
    return out


def build_conv_index(rules: List[dict]) -> Dict[str, List[dict]]:
    """conversation_id -> [rule, ...]"""
    idx: Dict[str, List[dict]] = defaultdict(list)
    for r in rules:
        for cid in r.get("supporting_trajectories", []) or []:
            if cid:
                idx[cid].append(r)
    return idx


# ── 归属判定 ───────────────────────────────────────────────────

def _is_agentrule(name: str) -> bool:
    return str(name or "").strip().lower() in AGENTRULE_TOKENS


def _top3_list(rule: dict) -> List[dict]:
    sa = rule.get("skill_attribution")
    if isinstance(sa, dict):
        t = sa.get("top3")
        if isinstance(t, list):
            return [x for x in t if isinstance(x, dict)]
    return []


def attribution_from_rule(rule: dict) -> Optional[dict]:
    """从单条 rule 的 skill_attribution.top3 取归属。无 top3 返回 None(转兜底)。"""
    top3 = _top3_list(rule)
    if not top3:
        return None
    top1 = top3[0]
    primary = (top1.get("skill_name") or "").strip()
    prob = top1.get("problematic_rule", "")
    conf = top1.get("confidence")
    if _is_agentrule(primary):
        # 附加: top3 第一个非空非 AgentRule skill
        secondary = None
        for t in top3[1:]:
            sn = (t.get("skill_name") or "").strip()
            if sn and not _is_agentrule(sn):
                secondary = sn
                break
        return {
            "attributed_to": "AgentRule",
            "is_agentrule": True,
            "secondary_skill": secondary,
            "confidence": conf,
            "problematic_rule": prob,
            "source": "rule_reverse_lookup",
        }
    return {
        "attributed_to": primary,
        "is_agentrule": False,
        "secondary_skill": None,
        "confidence": conf,
        "problematic_rule": prob,
        "source": "rule_reverse_lookup",
    }


def localize_with_rules(matched: List[dict]) -> Optional[dict]:
    """多条命中 rule 时, 取 top1 confidence 最高那条的归属; matched_rules 全留。"""
    best: Optional[dict] = None
    best_conf = -1.0
    for r in matched:
        a = attribution_from_rule(r)
        if a is None:
            continue
        c = a.get("confidence")
        try:
            cf = float(c) if c is not None else 0.0
        except (TypeError, ValueError):
            cf = 0.0
        if cf > best_conf:
            best_conf = cf
            best = a
    if best:
        best["matched_rules"] = [
            {"id": r.get("id", ""), "error_category": r.get("error_category", ""),
             "scoped_skill": r.get("scoped_skill", "")}
            for r in matched
        ]
    return best


def localize_with_scoped_skill(matched: List[dict]) -> Optional[dict]:
    """rule 无 skill_attribution(scoped phase6) 时, 退而用 rule 所属 skill 定位。
       能定到具体 skill 就用; 命中 AgentRule 性不明 -> 返回半成品(需 LLM 定附加)。"""
    for r in matched:
        sk = (r.get("scoped_skill") or "").strip()
        if sk and not _is_agentrule(sk):
            return {
                "attributed_to": sk,
                "is_agentrule": False,
                "secondary_skill": None,
                "confidence": None,
                "problematic_rule": r.get("error_reason", ""),
                "source": "rule_scoped_skill",
                "matched_rules": [{"id": r.get("id", ""), "error_category": r.get("error_category", ""),
                                    "scoped_skill": r.get("scoped_skill", "")}],
            }
    return None


# ── LLM 兜底(未归类 / scoped 无 attribution) ──────────────────

def llm_fallback(case: dict, trace: Optional[dict], skill_names: List[str],
                 agent_rule_desc: str) -> Optional[dict]:
    prompt = load_prompt(
        "fallback_attribution.txt",
        inputs="\n".join(case.get("inputs", []) or []),
        expected_behavior=case.get("expected_behavior", ""),
        reason=case.get("reason", ""),
        trajectory=trace_io.format_trajectory_text(trace) if trace else "(无可用 trace)",
        skill_list=", ".join(skill_names),
        agent_rule_desc=agent_rule_desc,
    )
    out = call_llm_json(call_llm, [
        {"role": "system", "content": "你是一名严谨的 agent 缺陷定位工程师。只输出 JSON。"},
        {"role": "user", "content": prompt},
    ])
    if not isinstance(out, dict):
        return None
    primary = (out.get("attributed_to") or out.get("skill") or "").strip()
    is_ar = bool(out.get("is_agentrule")) or _is_agentrule(primary)
    secondary = (out.get("secondary_skill") or "").strip() or None
    if is_ar:
        primary = "AgentRule"
        if secondary and _is_agentrule(secondary):
            secondary = None
    return {
        "attributed_to": primary or "AgentRule",
        "is_agentrule": is_ar,
        "secondary_skill": secondary,
        "confidence": None,
        "problematic_rule": out.get("reason", ""),
        "source": "llm_fallback",
        "matched_rules": [],
    }


# ── 主流程 ─────────────────────────────────────────────────────

def load_golden(path: Path) -> List[dict]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def build_trace_index(trace_dir: Path) -> Dict[str, dict]:
    idx = {}
    for t in trace_io.load_traces(trace_dir):
        cid = t.get("conversation_id")
        if cid:
            idx[cid] = t
    return idx


def list_skill_names(skills_flat: Path) -> List[str]:
    if not skills_flat.exists():
        return []
    names = set()
    for p in skills_flat.glob("*.md"):
        names.add(p.stem)
    for p in skills_flat.glob("*/SKILL.md"):
        names.add(p.parent.name)
    return sorted(names)


def localize_one(case: dict, conv_index, trace_index, skill_names, agent_rule_desc,
                 use_llm: bool) -> Optional[dict]:
    cid = case.get("id")
    matched = conv_index.get(cid, []) if cid else []
    if matched:
        loc = localize_with_rules(matched)
        if loc:
            return loc
        loc = localize_with_scoped_skill(matched)
        if loc:
            return loc
    if not use_llm:
        return {
            "attributed_to": "未归类",
            "is_agentrule": False,
            "secondary_skill": None,
            "confidence": None,
            "problematic_rule": "",
            "source": "unclassified_no_llm",
            "matched_rules": [],
        }
    trace = trace_index.get(cid) if cid else None
    return llm_fallback(case, trace, skill_names, agent_rule_desc)


# ── 汇总报告 ───────────────────────────────────────────────────

def aggregate(entries: List[dict]) -> dict:
    """entries = 带 localization 的失败 case。按 attributed_to 聚合。"""
    by_target = defaultdict(list)
    for e in entries:
        loc = e.get("localization") or {}
        by_target[loc.get("attributed_to", "未归类")].append(e)
    rows = []
    for target, cases in by_target.items():
        sec_counter = Counter()
        ar_cases = [c for c in cases if (c.get("localization") or {}).get("is_agentrule")]
        for c in ar_cases:
            s = (c.get("localization") or {}).get("secondary_skill")
            if s:
                sec_counter[s] += 1
        rows.append({
            "target": target,
            "count": len(cases),
            "pct": round(len(cases) / max(len(entries), 1) * 100, 1),
            "is_agentrule": bool(ar_cases),
            "top_secondary": sec_counter.most_common(1)[0][0] if sec_counter else None,
            "top_secondary_count": sec_counter.most_common(1)[0][1] if sec_counter else 0,
            "typical_cases": [c.get("script_id", c.get("id", "")) for c in cases[:3]],
        })
    rows.sort(key=lambda r: r["count"], reverse=True)
    return {"total_fail": len(entries), "rows": rows}


def write_jsonl(entries: List[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def write_md(summary: dict, path: Path):
    lines = ["# 故障定位报告 (fault localization)", "",
             f"失败/部分通过 case 共 **{summary['total_fail']}** 条。", "",
             "| 归属对象 | 命中数 | 占比 | 类型 | 最常连带 skill | 典型 case |",
             "|---|---:|---:|---|---|---|"]
    for r in summary["rows"]:
        typ = "AgentRule" if r["is_agentrule"] else "skill"
        sec = f"{r['top_secondary']} ({r['top_secondary_count']})" if r["top_secondary"] else "—"
        typ_cases = ", ".join(str(x) for x in r["typical_cases"]) or "—"
        lines.append(f"| {r['target']} | {r['count']} | {r['pct']}% | {typ} | {sec} | {typ_cases} |")
    lines.append("")
    lines.append("> 说明: 归属 AgentRule 的行, \"最常连带 skill\" = 除 AgentRule 外首个最常被点的 skill。")
    lines.append("> 单条 case 明细见 golden_localized.jsonl 的 localization 字段。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# ── CLI ────────────────────────────────────────────────────────

def parse_args():
    ap = argparse.ArgumentParser(description="失败 case 根因定位到 skill/AgentRule")
    ap.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))
    ap.add_argument("--golden", default=None, help="golden jsonl (默认 config paths.golden)")
    ap.add_argument("--rules", default=None, help="coldstart final_rules.json")
    ap.add_argument("--trace-dir", default=None, help="trace 目录(供 LLM 兜底)")
    ap.add_argument("--skills-flat", default=None, help="skills_flat 目录(供 LLM 兜底列 skill 名)")
    ap.add_argument("--no-llm", action="store_true", help="不调 LLM, 未归类 case 直接标'未归类'")
    ap.add_argument("--output", default=None, help="golden_localized.jsonl 输出路径")
    ap.add_argument("--report", default=None, help="localization_report.md 输出路径")
    ap.add_argument("--run-id", default=None)
    return ap.parse_args()


def main():
    import yaml
    args = parse_args()
    cfg = get_config()
    skill_cfg = yaml.safe_load((SKILL_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
    paths = skill_cfg.get("paths", {})

    golden_path = Path(args.golden or paths.get("golden", str(cfg.paths.golden)))
    rules_path = Path(args.rules or paths.get("rules", "data/runs/coldstart/merged/final_rules.json"))
    trace_dir = Path(args.trace_dir or paths.get("trace_dir", str(cfg.paths.traces)))
    skills_flat = Path(args.skills_flat or paths.get("skills_flat", str(cfg.paths.skills_flat)))
    out_jsonl = Path(args.output or paths.get("output", "data/golden/golden_localized.jsonl"))
    out_report = Path(args.report or paths.get("report", "data/golden/localization_report.md"))

    golden = load_golden(golden_path)
    rules = load_rules(rules_path)
    conv_index = build_conv_index(rules)
    trace_index = build_trace_index(trace_dir)
    skill_names = list_skill_names(skills_flat)
    agent_rule_path = _BUNDLE / "AgentRule.md"
    agent_rule_desc = agent_rule_path.read_text(encoding="utf-8")[:3000] if agent_rule_path.exists() else ""

    fail_results = set(skill_cfg.get("selection", {}).get("fail_results", ["失败", "部分通过"]))
    use_llm = not args.no_llm

    print(f"[定位] golden {len(golden)} 条, rules {len(rules)} 条, 反查索引 {len(conv_index)} 个对话, skills {len(skill_names)} 个")
    print(f"[定位] 定位范围 result ∈ {fail_results}, LLM 兜底={'开' if use_llm else '关'}")

    fail_entries = []
    for g in golden:
        if g.get("result") in fail_results:
            loc = localize_one(g, conv_index, trace_index, skill_names, agent_rule_desc, use_llm)
            g2 = dict(g)
            g2["localization"] = loc
            fail_entries.append(g2)
        else:
            g2 = dict(g)
            g2["localization"] = None
            g2["localization_note"] = "通过, 无需定位"

    # 输出: 全量 golden(保持原顺序, 同形 + localization 字段)
    all_entries = []
    for g in golden:
        if g.get("result") in fail_results:
            fe = next((e for e in fail_entries if e.get("id") == g.get("id")), g)
            all_entries.append(fe)
        else:
            g2 = dict(g)
            g2["localization"] = None
            g2["localization_note"] = "通过, 无需定位"
            all_entries.append(g2)
    write_jsonl(all_entries, out_jsonl)

    summary = aggregate(fail_entries)
    write_md(summary, out_report)
    state.save("fault_localization", "summary", summary, args.run_id)

    # 终端小结
    print(f"\n[完成] 失败/部分通过 {len(fail_entries)} 条已定位")
    print(f"  → {out_jsonl}")
    print(f"  → {out_report}")
    print("\n归属汇总:")
    for r in summary["rows"]:
        sec = f" (连带 {r['top_secondary']} x{r['top_secondary_count']})" if r["top_secondary"] else ""
        print(f"  {r['target']:30s} {r['count']:3d} ({r['pct']}%){sec}")


if __name__ == "__main__":
    main()
