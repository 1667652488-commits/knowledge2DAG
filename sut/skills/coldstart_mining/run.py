#!/usr/bin/env python3
"""
run_scoped_pipeline — skill-scoped 冷数据规则提取编排器

架构：
1. 全局 phase2（所有 trace 一起 → 全局检查点,跨 skill 通用）
2. skill-scoped phase2-6（按 badcase skill 分组,≥2 条才跑 → skill 特异 if-then 规则）
3. 合并：全局检查点 + skill 特异规则 → 最终规则集（JSON if-then + natural_language 双格式）

用法：
    python tools/run_scoped_pipeline.py \
        --trace-dir ../test_coldstartGen0627/runs/20260627_163420/traces \
        --golden ../test_coldstartGen0627/golden_output/golden_output_chosen_new0627.jsonl \
        --skills ../skill2sent \
        --output runs/$(date +%Y%m%d_%H%M%S) \
        --min-traces 2 --batch-size 10
"""

import json
import os
import sys
import argparse
import subprocess
from pathlib import Path
from collections import defaultdict


# ── pattern → skill 映射（可被 --grouping-file 覆盖）──
DEFAULT_PATTERN_SKILL = {
    "集团路由": "group_credit_risk",
    "路由": "group_credit_risk",
    "越界": "financial_report_identify",
    "职责边界越界": "financial_report_identify",
    "邮件总结": "mail_send",
    "数据透传-邮件总结原文": "mail_send",
    "期间矛盾": "financial_report_identify",
    "报告期间矛盾": "financial_report_identify",
    "narration伪ask": None,  # 伪问题,跳过 scoped
    "good": None,
    "good-ask_user": None,
}

TOOLS_DIR = Path(__file__).parent
PHASES_DIR = TOOLS_DIR / "phases"
_BUNDLE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BUNDLE_ROOT))

from common.llm_client import call_llm  # noqa: E402
from common.json_utils import is_llm_error  # noqa: E402


def load_traces(trace_dir: str) -> list:
    """加载目录下所有 trace_*.json(messages 格式)。"""
    traces = []
    for f in sorted(Path(trace_dir).glob("trace_*.json")):
        try:
            t = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if t.get("messages"):
            traces.append(t)
    return traces


def load_golden(golden_path: str) -> dict:
    """加载 golden → {script_id: {pattern, skill, expected_behavior, result, reason}}。"""
    if not golden_path or not Path(golden_path).exists():
        return {}
    m = {}
    for line in open(golden_path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        sid = r.get("script_id", "")
        pattern = r.get("pattern", "")
        skill = DEFAULT_PATTERN_SKILL.get(pattern)
        m[sid] = {
            "pattern": pattern,
            "skill": skill,
            "expected_behavior": r.get("expected_behavior", ""),
            "result": r.get("result", ""),
            "reason": r.get("reason", ""),
        }
    return m


def load_grouping_file(path: str) -> dict:
    """加载自定义分组 {script_id: skill_name}。"""
    if not path or not Path(path).exists():
        return {}
    return json.load(open(path, encoding="utf-8"))


def infer_skill_from_trace(trace: dict) -> str:
    """从 trace 的 tool_calls 推断 badcase skill（无 golden 时的 fallback）。"""
    msgs = trace.get("messages", [])
    for m in msgs:
        if m.get("role") in ("assistant", "agent"):
            tc = m.get("tool_calls") or []
            for t in tc:
                fn = t.get("function", {})
                name = fn.get("name", "")
                args_str = fn.get("arguments", "")
                try:
                    args = json.loads(args_str) if args_str else {}
                except Exception:
                    args = {}
                intent = args.get("query_intent", "")
                if intent:
                    # 从 intent 推断 skill
                    if "集团" in intent:
                        return "group_credit_risk"
                    elif "单客户" in intent:
                        return "single_credit_risk"
                    elif "财报" in intent or "financial" in intent.lower():
                        return "financial_report_identify"
                    elif "合规" in intent:
                        return "compliance_review"
                    elif "信贷知识" in intent or "credit_knowledge" in intent.lower():
                        return "credit_knowledge"
                    elif "分层" in intent:
                        return "customer_tiering"
                    elif "行业" in intent:
                        return "industry_classification"
                    elif "123" in intent:
                        return "corporate_credit_123"
            # call_mcp / execute_cmd → mail_send
            for t in tc:
                fn = t.get("function", {})
                if fn.get("name") in ("call_mcp", "execute_cmd"):
                    return "mail_send"
    return "unknown"


def group_by_skill(traces: list, golden: dict, grouping_file: dict,
                   min_traces: int = 2) -> tuple:
    """按 badcase skill 分组。

    Returns:
        (groups: {skill: [traces]}, ungrouped: [traces])
        ungrouped = 不足 min_traces 的 + 无法分组的（进全局 only）
    """
    custom = grouping_file or {}
    groups = defaultdict(list)

    for t in traces:
        sid = t.get("script_id", "")
        # 优先用自定义分组
        if sid in custom:
            skill = custom[sid]
        elif sid in golden and golden[sid].get("skill"):
            skill = golden[sid]["skill"]
        else:
            skill = infer_skill_from_trace(t)

        if skill and skill != "unknown":
            groups[skill].append(t)
        # skill=None（伪问题/good）→ 不进 scoped

    # 分离不足 min_traces 的
    valid_groups = {}
    ungrouped = []
    for skill, ts in groups.items():
        if len(ts) >= min_traces:
            valid_groups[skill] = ts
        else:
            ungrouped.extend(ts)

    return dict(valid_groups), ungrouped


def run_phase(cmd: list, name: str) -> bool:
    """执行一个 phase 脚本。cmd[1] 是脚本名(如 phase2_induct_linkage.py),改为绝对路径。"""
    # 把脚本名改为绝对路径(在 tools/ 下)
    if len(cmd) >= 2 and not os.path.isabs(cmd[1]):
        cmd[1] = str(PHASES_DIR / cmd[1])
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    print(f"  命令: {' '.join(cmd)}")
    v2_root = _BUNDLE_ROOT
    result = subprocess.run(cmd, cwd=str(v2_root))
    if result.returncode != 0:
        print(f"  ✗ {name} 失败 (exit {result.returncode})")
        return False
    print(f"  ✓ {name} 完成")
    return True


def run_global_phase2(traces: list, skills_dir: str, output_dir: str,
                      batch_size: int) -> dict:
    """全局 phase2：所有 trace 一起 → 全局检查点。"""
    # 写临时 trace 文件（全局）
    global_trace_dir = Path(output_dir) / "global_traces"
    global_trace_dir.mkdir(parents=True, exist_ok=True)
    for t in traces:
        sid = t.get("script_id", "unknown")
        with open(global_trace_dir / f"trace_{sid}.json", "w", encoding="utf-8") as f:
            json.dump(t, f, ensure_ascii=False, indent=2)

    phase2_output = str(Path(output_dir) / "global_phase2output.json")
    phase2_inter = str(Path(output_dir) / "global_phase2_intermediate")

    cmd = [
        sys.executable, "phase2.py",
        "--input", str(global_trace_dir),
        "--output", phase2_output,
        "--intermediate-dir", phase2_inter,
        "--batch-size", str(batch_size),
    ]
    run_phase(cmd, "全局 Phase 2: 归纳正确链路 + 缺失检查点（所有 trace）")

    if Path(phase2_output).exists():
        return json.load(open(phase2_output, encoding="utf-8"))
    return {}


def run_scoped_phase2_6(skill: str, traces: list, skills_dir: str,
                        output_dir: str, batch_size: int) -> dict:
    """skill-scoped phase2-6：一组 trace → skill 特异规则。"""
    skill_dir = Path(output_dir) / skill
    skill_dir.mkdir(parents=True, exist_ok=True)

    # 写该组的 trace 文件
    trace_dir = skill_dir / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    for t in traces:
        sid = t.get("script_id", "unknown")
        with open(trace_dir / f"trace_{sid}.json", "w", encoding="utf-8") as f:
            json.dump(t, f, ensure_ascii=False, indent=2)

    inter_dir = skill_dir / "intermediate"
    inter_dir.mkdir(parents=True, exist_ok=True)

    p2_out = str(skill_dir / "phase2output.json")
    p3_out = str(skill_dir / "phase3output.json")
    p4_out = str(skill_dir / "phase4output.json")
    p4_report = str(skill_dir / "feature_report.json")
    p4_log = str(skill_dir / "llm_judge_logs.jsonl")
    p4_cache = str(skill_dir / "llm_judge_cache.json")
    rules_out = str(skill_dir / "rules.json")
    rules_nl = str(skill_dir / "rules_natural_language.txt")
    rules_ranked = str(skill_dir / "rules_ranked.json")

    # phase2
    run_phase([
        sys.executable, "phase2.py",
        "--input", str(trace_dir), "--output", p2_out,
        "--intermediate-dir", str(inter_dir / "phase2_intermediate"),
        "--batch-size", str(batch_size),
    ], f"[{skill}] Phase 2: 归纳正确链路 + 缺失检查点")

    # phase3
    run_phase([
        sys.executable, "phase3.py",
        "--phase2-result", p2_out, "--trajectories", str(trace_dir),
        "--output", p3_out, "--batch-size", str(batch_size),
    ], f"[{skill}] Phase 3: 归纳类别 + 检查点")

    # phase4
    run_phase([
        sys.executable, "phase4.py",
        "--trajectories", str(trace_dir), "--categories", p3_out,
        "--output", p4_out, "--report", p4_report,
        "--log", p4_log, "--cache", p4_cache,
        "--batch-size", "5", "--max-turns", "10",
        "--trajectory-config", str(PHASES_DIR / "messages_trajectory_config.json"),
    ], f"[{skill}] Phase 4: LLM 特征提取")

    # phase5
    run_phase([
        sys.executable, "phase5.py",
        "--features", p4_out, "--categories", p3_out,
        "--trajectories", str(trace_dir), "--skills", skills_dir,
        "--output", rules_out,
        "--cache-dir", str(inter_dir / "phase5_cache"),
    ], f"[{skill}] Phase 5: 规则挖掘")

    # phase6
    run_phase([
        sys.executable, "phase6.py",
        "--rules", rules_out, "--categories", p3_out,
        "--trajectories", str(trace_dir), "--skills", skills_dir,
        "--output", rules_nl, "--output-json", rules_ranked,
    ], f"[{skill}] Phase 6: 规则语言化 + 排序")

    # 读结果
    result = {"skill": skill, "trace_count": len(traces), "trace_ids": [t.get("script_id") for t in traces]}
    if Path(rules_ranked).exists():
        result["rules"] = json.load(open(rules_ranked, encoding="utf-8"))
    if Path(rules_nl).exists():
        result["rules_nl"] = open(rules_nl, encoding="utf-8").read()
    return result


# ── 第二版 NL 规则: 诊断规则块(喂入 3 维评估器 {diagnostic_rules} 槽位) ──

def _collect_ranked_rules(scoped_results: list) -> list:
    """从 scoped_results 收集所有 accepted 规则(phase6 ranked JSON)。"""
    out = []
    for r in scoped_results or []:
        skill = r.get("skill", "")
        rules_obj = r.get("rules")
        if isinstance(rules_obj, dict):          # phase6: {meta, rules:[...], rejected_rules}
            rl = rules_obj.get("rules", [])
        elif isinstance(rules_obj, list):
            rl = rules_obj
        else:
            rl = []
        for rule in rl:
            if not isinstance(rule, dict):
                continue
            rule = dict(rule)
            rule.setdefault("_scoped_skill", skill)
            out.append(rule)
    return out


def _rule_digest(rule: dict) -> dict:
    """压缩单条规则为 LLM 改写所需最小字段。"""
    sa = rule.get("skill_attribution") or {}
    top3 = sa.get("top3") if isinstance(sa, dict) else None
    cond_text = []
    for c in rule.get("if_conditions") or []:
        if isinstance(c, dict):
            cond_text.append({
                "检查特征": c.get("feature_description") or c.get("feature", ""),
                "判定标准": c.get("judgment_criteria", ""),
            })
    return {
        "id": rule.get("id", ""),
        "error_category": rule.get("error_category") or rule.get("then_category", ""),
        "error_reason": rule.get("error_reason", ""),
        "检查条件": cond_text,
        "skill_attribution_top3": top3 or [],
        "scoped_skill": rule.get("_scoped_skill", ""),
    }


def _diagnostic_mechanical(digest: list) -> str:
    """LLM 失败/mock 时的机械兜底: 直接把规则罗列为诊断线索。"""
    lines = ["# 该 agent 诊断规则（塞入评估器 diagnostic_rules 槽位）", ""]
    for d in digest:
        lines.append(f"[{d['id']}] {d['error_category']}")
        lines.append(f"- 失败模式: {d['error_reason']}")
        for c in d["检查条件"]:
            lines.append(f"- 检查点: {c.get('检查特征', '')} → {c.get('判定标准', '')}")
        t3 = d.get("skill_attribution_top3") or []
        if t3:
            parts = []
            for t in t3:
                if isinstance(t, dict):
                    sn = t.get("skill_name") or "AgentRule"
                    pr = (t.get("problematic_rule") or "")[:60]
                    parts.append(f"{sn}({pr}, conf={t.get('confidence')})")
            if parts:
                lines.append("- 归因: " + " | ".join(parts))
        lines.append("")
    return "\n".join(lines)


def generate_diagnostic_block(scoped_results: list, use_llm: bool = True) -> str:
    """把 merged rules 用 LLM 改写成诊断规则块文本; LLM 失败/mock 降级机械罗列。"""
    rules = _collect_ranked_rules(scoped_results)
    if not rules:
        return ""
    digest = [_rule_digest(r) for r in rules]
    if not use_llm:
        return _diagnostic_mechanical(digest)
    prompt = (TOOLS_DIR / "prompts" / "diagnostic_rewrite.txt").read_text(encoding="utf-8")
    prompt = prompt.replace("{rules_json}", json.dumps(digest, ensure_ascii=False, indent=2))
    raw = call_llm([
        {"role": "system", "content": "你是 agent 评估规则改写工程师。只输出纯文本诊断规则块。"},
        {"role": "user", "content": prompt},
    ])
    if is_llm_error(raw) or not (raw or "").strip():
        print("  [诊断规则] LLM 失败/mock, 降级机械罗列")
        return _diagnostic_mechanical(digest)
    return raw.strip()


def write_enhanced_prompt(diagnostic_block: str, out_path):
    """把诊断块填入 base prompt 的 {diagnostic_rules}, 写出完整增强评估 prompt。
    其它占位符({expected_section}/{skill_names_section}/{skill_names}/{messages})保留, 评估时填。"""
    base = (TOOLS_DIR / "templates" / "evaluator_base_prompt.txt").read_text(encoding="utf-8")
    block = diagnostic_block or "(该 agent 暂无挖掘出的诊断规则)"
    enhanced = base.replace("{diagnostic_rules}", block)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(enhanced, encoding="utf-8")


def merge_rules(global_phase2: dict, scoped_results: list, output_dir: str):
    """合并全局检查点 + skill 特异规则 → 最终规则集（JSON + natural_language + 诊断规则块）。"""
    merged_dir = Path(output_dir) / "merged"
    merged_dir.mkdir(parents=True, exist_ok=True)

    # 合并 JSON
    merged = {
        "global_checkpoints": global_phase2.get("phase2_output", global_phase2),
        "scoped_rules": [r for r in scoped_results if r.get("rules") or r.get("rules_nl")],
    }
    json.dump(merged, open(merged_dir / "final_rules.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    # 合并 natural_language
    nl_parts = ["# 最终规则集（全局检查点 + skill 特异规则）\n"]
    nl_parts.append("## 一、全局检查点（跨 skill 通用）\n")
    gp = global_phase2.get("phase2_output", global_phase2)
    if isinstance(gp, dict):
        for cp in gp.get("missing_checkpoints_list", []):
            nl_parts.append(f"- [{cp.get('severity','')}] {cp.get('description','')}\n")
    nl_parts.append("\n---\n")

    for r in scoped_results:
        skill = r.get("skill", "?")
        nl = r.get("rules_nl", "")
        if nl:
            nl_parts.append(f"\n## {skill} 特异规则（{r.get('trace_count',0)} trace）\n")
            nl_parts.append(nl)
            nl_parts.append("\n---\n")

    open(merged_dir / "final_rules_natural_language.txt", "w", encoding="utf-8").write("".join(nl_parts))

    # 第二版 NL 规则: 诊断规则块(喂入 3 维评估器 {diagnostic_rules}) + 完整增强评估 prompt
    try:
        use_llm = not os.environ.get("COLDSTART_NO_LLM_DIAG")
        diag_block = generate_diagnostic_block(scoped_results, use_llm=use_llm)
        if diag_block:
            (merged_dir / "final_rules_diagnostic.txt").write_text(diag_block, encoding="utf-8")
            write_enhanced_prompt(diag_block, merged_dir / "evaluator_prompt_enhanced.md")
            print(f"  诊断规则块: {merged_dir / 'final_rules_diagnostic.txt'}")
            print(f"  增强评估prompt: {merged_dir / 'evaluator_prompt_enhanced.md'}")
    except Exception as e:
        print(f"  [诊断规则] 生成跳过: {e}")

    print(f"\n{'='*60}")
    print(f"  ✓ 最终规则已合并")
    print(f"{'='*60}")
    print(f"  全局检查点: {merged_dir / 'final_rules.json'}")
    print(f"  自然语言: {merged_dir / 'final_rules_natural_language.txt'}")
    for r in scoped_results:
        skill = r.get("skill", "?")
        print(f"  [{skill}] {r.get('trace_count',0)} trace → rules: {len(r.get('rules', {}).get('rules', []))} 条")


def main():
    parser = argparse.ArgumentParser(description="skill-scoped 冷数据规则提取")
    parser.add_argument("--trace-dir", required=True, help="trace 目录")
    parser.add_argument("--golden", default=None, help="golden output JSONL（pattern→skill 分组用）")
    parser.add_argument("--grouping-file", default=None, help="自定义分组 {script_id: skill}")
    parser.add_argument("--skills", default="skills_flat", help="skill 目录(扁平 .md 文件,非嵌套)")
    parser.add_argument("--output", default="runs/scoped_run", help="输出目录")
    parser.add_argument("--min-traces", type=int, default=2, help="每组最少 trace 数（不足并全局）")
    parser.add_argument("--batch-size", type=int, default=10, help="phase2/3/4 批大小")
    parser.add_argument("--skip-global", action="store_true", help="跳过全局 phase2")
    parser.add_argument("--skip-scoped", action="store_true", help="跳过 skill-scoped phase2-6")
    args = parser.parse_args()

    # 加载
    traces = load_traces(args.trace_dir)
    golden = load_golden(args.golden)
    grouping = load_grouping_file(args.grouping_file)
    print(f"加载 {len(traces)} 条 trace | golden {len(golden)} 条 | 自定义分组 {len(grouping)} 条")

    # 分组
    groups, ungrouped = group_by_skill(traces, golden, grouping, args.min_traces)
    print(f"\n分组结果（≥{args.min_traces} 条/组）:")
    for skill, ts in groups.items():
        ids = [t.get("script_id") for t in ts]
        print(f"  {skill}: {len(ts)} 条 {ids}")
    if ungrouped:
        print(f"  未分组（进全局 only）: {[t.get('script_id') for t in ungrouped]}")

    output_dir = args.output
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # 1. 全局 phase2
    global_phase2 = {}
    if not args.skip_global:
        global_phase2 = run_global_phase2(traces, args.skills, output_dir, args.batch_size)

    # 2. skill-scoped phase2-6
    scoped_results = []
    if not args.skip_scoped:
        for skill, ts in groups.items():
            result = run_scoped_phase2_6(skill, ts, args.skills, output_dir, args.batch_size)
            scoped_results.append(result)

    # 3. 合并
    merge_rules(global_phase2, scoped_results, output_dir)


if __name__ == "__main__":
    main()
