#!/usr/bin/env python3
"""
scorer — 用「评估器基础提示词」对单条轨迹打分(独立于 golden)。

打 3 维度分(task_completion / trajectory_quality / safety) + is_pass + score +
attributed_skill + reason, 输出 JSONL。在 golden 之前跑, 结果可选喂给 golden 作参考。

{expected_section} / {diagnostic_rules} 留空(无预设预期行为, 评估器靠用户消息理解目标;
diagnostic_rules 此时 coldstart 还没跑)。{skill_names} 填 skills_flat 的 skill 名单(归因用)。

用法:
    python run.py score --trace-dir runs/<RUN>/agent/<ts>/traces --output runs/<RUN>/scores/scores.jsonl
    python run.py score --trace-dir <TD> --skills data/skills_flat
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_BUNDLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BUNDLE))

from common import trace_io  # noqa: E402
from common.json_utils import extract_json_block  # noqa: E402
from common.llm_client import call_llm  # noqa: E402


# ── 提示词: 运行时从 docs/评估器基础提示词 读(唯一源, 改 docs 即生效) ──────────
PROMPT_FILE = _BUNDLE / "docs" / "评估器基础提示词"


def load_prompt() -> str:
    """读 docs/评估器基础提示词 作为提示词模板。模板含 {expected_section} 等 slot,
    用 str.replace 填充(不用 str.format, 避免模板里 JSON 示例的 {} 被误解析)。"""
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(
            f"评估器提示词文件不存在: {PROMPT_FILE}\n"
            "请确认 docs/评估器基础提示词 在位(它是 scorer 的唯一提示词源)。")
    return PROMPT_FILE.read_text(encoding="utf-8")


def load_skill_names(skill_dir: str) -> str:
    """读 skills_flat 下每个 .md 的 name + description 首行, 拼成列表文本。"""
    if not skill_dir:
        return ""
    lines = []
    for md in sorted(Path(skill_dir).glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        name = md.stem
        m = re.search(r"description:\s*>?\s*\n?\s*(.+)", text)
        desc = (m.group(1).strip()[:60] if m else "")
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def format_messages(msgs: list) -> str:
    """轨迹消息 → 可读文本(role/content/tool_calls)。"""
    out = []
    for m in msgs:
        role = m.get("role", "?")
        parts = [f"[{role}]"]
        c = str(m.get("content", "") or "")
        if c:
            parts.append(c)
        tcs = m.get("tool_calls") or []
        for tc in tcs:
            fn = (tc.get("function") or {}).get("name", "?")
            args = (tc.get("function") or {}).get("arguments", "")
            parts.append(f"<tool_call:{fn} args={str(args)[:200]}>")
        if m.get("reasoning_content"):
            parts.append(f"<think:{str(m.get('reasoning_content'))[:120]}>")
        out.append(" ".join(parts))
    return "\n".join(out)


def score_trace(trace: dict, skill_names: str) -> dict:
    """对单条轨迹打分, 返回 {script_id, task_completion, ...}。"""
    sid = trace.get("script_id", "")
    conv_id = trace.get("conversation_id", "")
    msgs = trace_io.get_messages(trace)
    if not msgs:
        return {"script_id": sid, "conversation_id": conv_id, "error": "无 messages"}

    # 提示词从 docs/评估器基础提示词 读(唯一源); 用 str.replace 填 slot
    # (不用 str.format, 避免模板里 JSON 示例的 {} 被误解析)
    prompt = load_prompt()
    prompt = (prompt
              .replace("{expected_section}", "")        # 留空: 无预设预期行为
              .replace("{skill_names_section}", "")     # 留空
              .replace("{diagnostic_rules}", "")        # 留空: coldstart 还没跑
              .replace("{skill_names}", skill_names or "(未提供)")
              .replace("{messages}", format_messages(msgs)))
    raw = call_llm([{"role": "user", "content": prompt}])
    try:
        data = extract_json_block(raw)
    except Exception:
        data = None
    if not isinstance(data, dict):
        return {"script_id": sid, "conversation_id": conv_id,
                "error": f"LLM 未返回合法 JSON: {raw[:200]}"}

    data.setdefault("script_id", sid)
    data.setdefault("conversation_id", conv_id)
    return data


def process_batch(trace_dir: str, output_path: str, skill_dir: str) -> None:
    skill_names = load_skill_names(skill_dir)
    tfiles = sorted(Path(trace_dir).glob("trace_*.json"))
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    ok = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for i, tf in enumerate(tfiles, 1):
            trace = json.loads(tf.read_text(encoding="utf-8"))
            sid = trace.get("script_id", tf.stem)
            print(f"[{i}/{len(tfiles)}] {sid} ...", flush=True)
            rec = score_trace(trace, skill_names)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if "error" in rec:
                print(f"  ✗ {rec['error'][:120]}")
            else:
                ok += 1
                print(f"  ✓ pass={rec.get('is_pass')} score={rec.get('score')} "
                      f"tc={rec.get('task_completion')} tq={rec.get('trajectory_quality')} saf={rec.get('safety')}")
    print(f"\n共 {len(tfiles)} | 成功 {ok} | 输出 {output_path}")


def main():
    from common.config import get_config
    cfg = get_config()
    ap = argparse.ArgumentParser(description="scorer — 评估器基础提示词打分(独立于 golden)")
    ap.add_argument("--trace-dir", default=str(cfg.paths.traces))
    ap.add_argument("--output", default=str(cfg.paths.golden / "scores.jsonl"))
    ap.add_argument("--skill-dir", default=str(cfg.paths.skills_flat))
    args = ap.parse_args()
    process_batch(args.trace_dir, args.output, args.skill_dir)


if __name__ == "__main__":
    main()
