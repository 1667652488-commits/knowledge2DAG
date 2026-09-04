#!/usr/bin/env python3
"""
badcase_script_gen — 剧本生成(messages-only, common 接入)。

工作流:
  阶段 0: 读 SKILL/AgentRule → LLM 提炼弱点清单(按 skill 切片, 防上下文爆炸)
  阶段 1: 生成覆盖矩阵
  阶段 2: 弱点+矩阵 → LLM 生成剧本
  阶段 3: LLM 审核剧本
  输出: data/results/badcase_scripts.json

用法:
    python -m skills.badcase_script_gen.run
    python -m skills.badcase_script_gen.run --skip-phase0 --weakness-file weakness.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

_BUNDLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BUNDLE))

from common.llm_client import call_llm  # noqa: E402
from common.json_utils import extract_json_block, is_llm_error, call_llm_json  # noqa: E402
from common import state  # noqa: E402
from common.config import get_config  # noqa: E402

SKILL_DIR = Path(__file__).parent


def load_prompt(name: str, **variables: Any) -> str:
    text = (SKILL_DIR / "prompts" / name).read_text(encoding="utf-8")
    for k, v in variables.items():
        text = text.replace(f"{{{{{k}}}}}", str(v))
    return text


def parse_args():
    ap = argparse.ArgumentParser(description="badcase 剧本生成器")
    ap.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--skip-phase0", action="store_true")
    ap.add_argument("--weakness-file", default=None)
    ap.add_argument("--target-count", type=int, default=None,
                    help="生成剧本数量(覆盖 config 的 generation.target_count)")
    ap.add_argument("--batch-size", type=int, default=None,
                    help="phase2 每批生成条数(默认 10); 一次生成超 max_tokens 截断时分批")
    ap.add_argument("--skip-phase3", action="store_true",
                    help="跳过 phase3 审核(100 条时 phase3 输入输出巨大, 易慢/截断, 可跳过直接输出 raw)")
    return ap.parse_args()


def read_skill_docs(skill_root: Path) -> List[Dict[str, str]]:
    """按 skill 逐个读 SKILL.md(切片, 防上下文爆炸)。返回 [{skill, content}]。

    优先读扁平 <name>.md(golden_gen 的 data/skills_cache 格式); 回退子目录 <name>/SKILL.md。
    对公仅 4 个 skill, 全量读不截断(主子agent架构需完整 description/步骤才能找弱点)。
    """
    out = []
    if not skill_root.exists():
        return out
    # 1) 扁平 <name>.md(cache 格式); 跳过根 SKILL.md 与 AgentRule.md(非 skill, 另由 agent_rule 路径读)
    for p in sorted(skill_root.glob("*.md")):
        if p.name in ("SKILL.md", "AgentRule.md"):
            continue
        try:
            out.append({"skill": p.stem, "content": p.read_text(encoding="utf-8")})
        except Exception:
            continue
    # 2) 回退子目录 <name>/SKILL.md(旧 skills_flat 双重布局)
    if not out:
        for sk_md in sorted(skill_root.glob("*/SKILL.md")):
            try:
                out.append({"skill": sk_md.parent.name, "content": sk_md.read_text(encoding="utf-8")})
            except Exception:
                continue
    if not out and (skill_root / "SKILL.md").exists():
        out.append({"skill": skill_root.name, "content": (skill_root / "SKILL.md").read_text(encoding="utf-8")})
    return out


def read_scenario_rules(scenarios_dir: Path) -> str:
    """读 scenarios_dir 下所有 AgentRule_*.md, 拼成带标注块(对公主子 dispatch/子Agent 规则)。

    来源: EDPAgent 部署 assets 的 skills/scenarios/(本地 d:/edpagent并行版0729/.../scenarios,
    已拷到 data/skills_cache/scenarios)。这些场景规则描述主Agent(call_multiagent 调度)/
    子Agent(call_multiversatile 并行工作流)的路径规划, 是 phase0 找主子断点的关键输入。
    """
    blocks = []
    if not scenarios_dir or not scenarios_dir.exists():
        return ""
    for p in sorted(scenarios_dir.glob("AgentRule_*.md")):
        try:
            blocks.append(f"===== 场景规则: {p.stem} =====\n{p.read_text(encoding='utf-8')}\n")
        except Exception:
            continue
    return "\n".join(blocks)


def phase0_understand(skill_root: Path, agent_rule_path: Path, run_id,
                      scenarios_dir: Path = None) -> List[Dict]:
    """阶段 0: 按 skill 分批提炼弱点(每批 5 个 skill, 防上下文爆炸)。

    docs = AgentRule(框架, 可选) + 场景规则 AgentRule_*.md(对公主子 dispatch/子Agent) + 各 SKILL.md。
    """
    print("[阶段 0] 读取 SKILL/AgentRule/场景规则 并提炼弱点清单...")
    skills = read_skill_docs(skill_root)
    agent_rule = ""
    if agent_rule_path.exists():
        agent_rule = agent_rule_path.read_text(encoding="utf-8")[:3000]
    scenario_rules = read_scenario_rules(scenarios_dir) if scenarios_dir else ""
    if scenario_rules:
        print(f"  场景规则: {len(scenario_rules.split('===== 场景规则:')) - 1} 份 AgentRule_*.md")

    all_weaknesses: List[Dict] = []
    batch_size = 5
    for i in range(0, len(skills), batch_size):
        batch = skills[i:i + batch_size]
        docs = ""
        if agent_rule:
            docs += f"===== AgentRule(框架) =====\n{agent_rule}\n\n"
        if scenario_rules:
            docs += scenario_rules + "\n\n"
        for s in batch:
            docs += f"===== SKILL: {s['skill']} =====\n{s['content']}\n\n"
        prompt = load_prompt("phase0_weakness_finding.txt", documents=docs)
        raw = call_llm([
            {"role": "system", "content": "你是一名严谨的 agent 测试工程师。只输出 JSON。"},
            {"role": "user", "content": prompt},
        ])
        if is_llm_error(raw):
            print(f"  批 {i//batch_size+1} LLM 失败, 跳过")
            continue
        try:
            w = extract_json_block(raw)
            if isinstance(w, list):
                all_weaknesses.extend(w)
            print(f"  批 {i//batch_size+1}: +{len(w) if isinstance(w,list) else 0} 弱点")
        except Exception as e:
            print(f"  批 {i//batch_size+1} 解析失败: {e}")
    state.save("badcase_script_gen", "weakness_list", all_weaknesses, run_id)
    print(f"[阶段 0] 提炼弱点 {len(all_weaknesses)} 条")
    return all_weaknesses


def phase1_coverage_matrix(weakness_list, skills, run_id) -> Dict:
    print("[阶段 1] 生成覆盖矩阵...")
    dimensions = ["HITL", "路由", "编排", "忠实性", "业务规则", "上下文", "越界"]
    covered = {(w.get("skill", ""), w.get("dimension", "")) for w in weakness_list}
    uncovered = [{"skill": s, "dimension": d} for s in skills for d in dimensions
                 if (s, d) not in covered]
    matrix = {"skills": skills, "dimensions": dimensions,
              "covered_count": len(covered), "uncovered_cells": uncovered}
    state.save("badcase_script_gen", "coverage_matrix", matrix, run_id)
    print(f"[阶段 1] 覆盖 {len(covered)}, 未覆盖 {len(uncovered)}")
    return matrix


def phase2_generate_scripts(weakness_list, matrix, run_id, target_count=20,
                             batch_size=10, max_tokens=8192) -> List[Dict]:
    """分批生成: 每批 batch_size 条, 多次 LLM 调用拼接, 防一次性 100 条超 max_tokens 截断。

    每批 prompt 的 target_count = min(batch_size, 剩余); 告知已生成 id 互补不重复。
    单批解析失败(截断/非JSON)跳过继续, 不阻断; 连续失败过多终止。
    """
    print(f"[阶段 2] 生成 badcase 剧本(目标 {target_count} 条, 每批 {batch_size} 条)...")
    samples = []
    for p in sorted((SKILL_DIR / "samples").glob("*.json"))[:3]:
        try:
            samples.append(f"===== {p.name} =====\n{p.read_text(encoding='utf-8')[:1500]}\n")
        except Exception:
            continue
    samples_str = "".join(samples)
    wl_json = json.dumps(weakness_list, ensure_ascii=False)
    mat_json = json.dumps(matrix, ensure_ascii=False)

    all_scripts: List[Dict] = []
    seen_ids: set = set()
    batch_num = 0
    fail_streak = 0
    while len(all_scripts) < target_count:
        batch_num += 1
        remaining = target_count - len(all_scripts)
        this_batch = min(batch_size, remaining)
        prompt = load_prompt("phase2_script_writing.txt",
                             weakness_list=wl_json, coverage_matrix=mat_json,
                             samples=samples_str, target_count=this_batch)
        # 告知已生成, 要求互补不重复(id 接续)
        if all_scripts:
            prompt += (
                f"\n\n## 已生成(勿重复, 互补)\n已生成 {len(all_scripts)} 条, id: {sorted(seen_ids)}。"
                f"本批新造 {this_batch} 条, id 从 BC{len(all_scripts)+1:03d} 起接续; "
                f"场景/措辞与已生成互补(换企业实体/换意图组合/换断点), 不重复。"
            )
        scripts = call_llm_json(call_llm, [
            {"role": "system", "content": "你是一名资深的 agent 测试用例设计师。只输出 JSON。"},
            {"role": "user", "content": prompt},
        ], max_tokens=max_tokens)
        if not isinstance(scripts, list):
            fail_streak += 1
            print(f"  批 {batch_num}: 解析失败(可能截断/非JSON), 跳过 (累计 {len(all_scripts)}/{target_count})")
            if fail_streak >= 3 or batch_num > target_count + 5:
                print("  连续失败过多或批次超限, 终止 phase2")
                break
            continue
        fail_streak = 0
        new = 0
        for s in scripts:
            if not isinstance(s, dict):
                continue
            sid = s.get("id") or f"BC{len(all_scripts)+1:03d}"
            if sid in seen_ids:
                sid = f"BC{len(all_scripts)+1:03d}"
            s["id"] = sid
            seen_ids.add(sid)
            all_scripts.append(s)
            new += 1
        print(f"  批 {batch_num}: +{new} (累计 {len(all_scripts)}/{target_count})")
    state.save("badcase_script_gen", "scripts_raw", all_scripts, run_id)
    print(f"[阶段 2] 生成剧本 {len(all_scripts)} 条")
    return all_scripts


def phase3_review(scripts, run_id) -> List[Dict]:
    print("[阶段 3] 审核剧本...")
    prompt = load_prompt("phase3_review.txt", scripts=json.dumps(scripts, ensure_ascii=False))
    review = call_llm_json(call_llm, [
        {"role": "system", "content": "你是一名严格的测试用例审核员。只输出 JSON。"},
        {"role": "user", "content": prompt},
    ])
    if not isinstance(review, dict):
        review = {}
    state.save("badcase_script_gen", "review", review, run_id)
    revised = review.get("revised_scripts", scripts)
    print(f"[阶段 3] approved={review.get('approved')}, issues={len(review.get('issues', []))}")
    return revised if isinstance(revised, list) else scripts


def main():
    args = parse_args()
    cfg = get_config()
    import yaml
    skill_cfg = yaml.safe_load((SKILL_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
    paths = skill_cfg.get("paths", {})
    skill_root = Path(paths.get("skill_root", str(cfg.paths.skills_flat)))
    agent_rule = Path(paths.get("agent_rule", str(cfg.paths.root / "AgentRule.md")))
    scenarios_dir = Path(paths.get("scenarios_dir", "")) or None
    output_dir = Path(paths.get("output_dir", str(cfg.paths.results)))
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.skip_phase0 and args.weakness_file:
        weakness_list = json.loads(Path(args.weakness_file).read_text(encoding="utf-8"))
        print(f"[阶段 0] 外部加载弱点 {len(weakness_list)} 条")
    elif args.skip_phase0:
        weakness_list = state.load("badcase_script_gen", "weakness_list", args.run_id)
    else:
        weakness_list = phase0_understand(skill_root, agent_rule, args.run_id,
                                           scenarios_dir=scenarios_dir)

    skills = sorted({w.get("skill", "未知") for w in weakness_list})
    matrix = phase1_coverage_matrix(weakness_list, skills, args.run_id)
    # 数量: CLI --target-count 优先, 否则 config generation.target_count, 默认 20
    gen_cfg = skill_cfg.get("generation", {})
    target_count = args.target_count if args.target_count is not None else int(gen_cfg.get("target_count", 20))
    batch_size = args.batch_size if args.batch_size is not None else int(gen_cfg.get("batch_size", 10))
    scripts = phase2_generate_scripts(weakness_list, matrix, args.run_id,
                                       target_count=target_count, batch_size=batch_size)
    if args.skip_phase3:
        print("[阶段 3] 跳过(--skip-phase3)")
        revised = scripts
    else:
        revised = phase3_review(scripts, args.run_id)

    out_file = output_dir / skill_cfg.get("generation", {}).get("output_filename", "badcase_scripts.json")
    out_file.write_text(json.dumps(revised, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[完成] {len(revised)} 条剧本 → {out_file}")


if __name__ == "__main__":
    main()
