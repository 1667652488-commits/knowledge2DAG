#!/usr/bin/env python3
"""
trace_to_markdown — 刷新版(读 cleaned-traces messages 格式), bundle 版(common 接入)

输入: trace_{id}.json (含 messages[{role,content,tool_calls,reasoning_content,...}])
输出: trace_{id}.md (概览 + 调用链摘要 + 对话 + 工具调用解析 + reasoning折叠)

刷新点(vs 旧版):
- 顶部加「调用链摘要」: 编号列出所有工具调用 + query_intent/skill, 一眼看清链路
- tool_call 参数解析: query_intent / script_command / script_params(双编码解开) / attachmentList 长度+预览
- 工具返回状态检测: success/fail/sandbox execution failed, 标色 + 长度
- attachmentList 解析失败时 regex 兜底抽长度(发现重试截断这类难定位问题)

用法:
    python -m skills.trace_md.run --trace data/traces/trace_F05.json --output data/md/trace_F05.md
    python -m skills.trace_md.run --trace-dir data/traces --output-dir data/md --golden data/golden/golden_output.jsonl
"""

import json
import argparse
import glob
import sys
from pathlib import Path

_BUNDLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BUNDLE))
from common.config import get_config  # noqa: E402


# ── 工具调用解析 ────────────────────────────────────────────

def _safe_json(s):
    """容错 json.loads(可能是 str/dict/空)。"""
    if isinstance(s, dict):
        return s
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def parse_tool_call(t: dict) -> dict:
    """解析一个 tool_call: name + 关键参数(query_intent/script_command/script_params/attachmentList)。"""
    fn = t.get("function", {}) or {}
    name = fn.get("name", "")
    raw_args = fn.get("arguments", "")
    args = _safe_json(raw_args)
    intent = args.get("query_intent", "")
    script_command = args.get("script_command", "")
    sp_raw = args.get("script_params", "")
    sp = _safe_json(sp_raw)  # call_mcp 的 script_params 是双编码 JSON 字符串
    attachment = sp.get("attachmentList") or sp.get("attachments") or sp.get("attachment_list")
    attachment_len = len(str(attachment)) if attachment is not None else None
    # 兜底: script_params 解析失败时, 用 regex 从原始 args 抽 attachmentList 长度
    if attachment is None and "attachmentList" in raw_args:
        seg = raw_args.split("attachmentList", 1)[-1][:2000]
        attachment_len = len(seg)
    return {
        "name": name,
        "intent": intent,
        "script_command": script_command,
        "script_params": sp,
        "attachment": attachment,
        "attachment_len": attachment_len,
        "raw_args": raw_args,
    }


def tool_result_status(content: str) -> tuple:
    """从 tool 返回内容推断状态: (status_tag, preview)。"""
    c = content or ""
    low = c.lower()
    if "sandbox execution failed" in low or "sandbox" in low:
        tag = "❌sandbox"
    elif '"status":"fail' in low or "'status': 'fail" in low or '"status": "fail' in low:
        tag = "❌fail"
    elif "failcause" in low or "fail_cause" in low:
        tag = "⚠failCause"
    elif "<agents." in c and "object at 0x" in c:
        tag = "ℹ对象引用"  # lite_todo 等返回内存地址
    elif '"status":"success' in low or "'status': 'success" in low or '"status": "success' in low:
        tag = "✅success"
    else:
        tag = "·"
    return tag, c[:160].replace("\n", " ")


def fmt_call_summary(tc: dict, n: int) -> str:
    """调用链摘要一行。"""
    p = parse_tool_call(tc)
    s = f"- #{n} `{p['name']}`"
    if p["intent"]:
        s += f"  intent=**{p['intent']}**"
    if p["script_command"]:
        sc = p["script_command"].split("/")[-1]
        s += f"  script={sc}"
    if p["attachment_len"] is not None:
        s += f"  attachmentList(≈{p['attachment_len']})"
    return s


def render_tool_call(tc: dict, n: int) -> str:
    """单条 tool_call 详细渲染。"""
    p = parse_tool_call(tc)
    lines = [f"**#{n} `{p['name']}`**"]
    if p["intent"]:
        lines.append(f"- query_intent: `{p['intent']}`")
    if p["script_command"]:
        lines.append(f"- script_command: `{p['script_command']}`")
    if p["script_params"]:
        sp = p["script_params"]
        keys = [k for k in ("mailTo", "applyUser", "applyBranch", "content") if k in sp]
        if keys:
            kv = ", ".join(f"{k}={str(sp[k])[:40]!r}" for k in keys)
            lines.append(f"- script_params: {kv}")
        if p["attachment"] is not None:
            att = str(p["attachment"])
            if len(att) > 120:
                lines.append(f"- attachmentList: len={len(att)} | 预览: `{att[:120]}...`")
            else:
                lines.append(f"- attachmentList: len={len(att)} | `{att}`")
    elif p["attachment_len"] is not None:
        lines.append(f"- attachmentList(解析失败, 估算 ≈{p['attachment_len']} 字, 见 raw args)")
        raw = p["raw_args"]
        lines.append(f"- args: `{raw[:300]}{'...' if len(raw) > 300 else ''}`")
    else:
        raw = p["raw_args"]
        if raw:
            lines.append(f"- args: `{raw[:200]}{'...' if len(raw) > 200 else ''}`")
    return "\n".join(lines)


# ── message 渲染 ────────────────────────────────────────────

ROLE_MAP = {"user": "👤 用户", "assistant": "🤖 Agent", "tool": "🔧 工具结果"}


def message_to_md(m: dict, idx: int, call_counter: list) -> str:
    """单条 message → markdown 段。call_counter 是可变计数器([n])。"""
    role = m.get("role", "")
    content = str(m.get("content", "") or "")
    label = ROLE_MAP.get(role, role)
    lines = [f"### msg[{idx}] {label}"]

    if content:
        if role == "tool":
            tag, preview = tool_result_status(content)
            lines.append(f"`{tag}` len={len(content)} | {preview}{'...' if len(content) > 160 else ''}")
        else:
            lines.append(content)

    tc = m.get("tool_calls")
    if tc:
        lines.append(f"\n**工具调用({len(tc)}次):**")
        for t in tc:
            call_counter[0] += 1
            lines.append(render_tool_call(t, call_counter[0]))

    rc = m.get("reasoning_content", "")
    if rc:
        lines.append(f"\n<details><summary>🧠 reasoning ({len(rc)}字)</summary>\n\n{rc}\n\n</details>")

    tcid = m.get("tool_call_id")
    if tcid:
        lines.append(f"\n*(tool_call_id: `{tcid}`)*")

    return "\n".join(lines)


# ── 主转换 ──────────────────────────────────────────────────

def trace_to_md(trace, output_path: str = None, golden_map: dict = None) -> str:
    """trace(json path 或 dict) → markdown。"""
    if isinstance(trace, str):
        t = json.load(open(trace, encoding="utf-8"))
    else:
        t = trace
    sid = t.get("script_id", "")
    cid = t.get("conversation_id", "")
    msgs = t.get("messages", [])
    errors = t.get("errors", [])
    script = t.get("script", {}) or {}

    lines = [f"# 轨迹报告 · `{sid}`", ""]
    lines.append(f"> conversation_id: `{cid}`")
    lines.append(f"> messages: {len(msgs)} | errors: {errors or '无'}")
    if script:
        cat = script.get("category", "")
        turns = script.get("fixed_turns", [])
        lines.append(f"> category: {cat} | skill: {script.get('skill','')} | turns: {len(turns)}")
        if turns:
            lines.append(f"> 用户输入: {turns[0][:80]}")
    if golden_map and sid in golden_map:
        g = golden_map[sid]
        lines.append(f">\n> **golden 判定**: **{g.get('result', '?')}**")
        lines.append(f"> expected_behavior: {g.get('expected_behavior', '')[:120]}")
        lines.append(f"> reason: {g.get('reason', '')[:120]}")
    lines.append("")

    # 调用链摘要(顶部一眼看清)
    all_tc = [(i, t) for i, m in enumerate(msgs) if m.get("role") == "assistant"
              for t in (m.get("tool_calls") or [])]
    if all_tc:
        lines.append("## 调用链摘要")
        for n, (_, tc) in enumerate(all_tc, 1):
            lines.append(fmt_call_summary(tc, n))
        lines.append(f"\n*共 {len(all_tc)} 次工具调用*\n")

    lines.append("---\n")

    call_counter = [0]
    for i, m in enumerate(msgs):
        lines.append(message_to_md(m, i, call_counter))
        lines.append("")

    md = "\n".join(lines)
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        open(output_path, "w", encoding="utf-8").write(md)
    return md


def load_golden_map(golden_path: str) -> dict:
    if not golden_path or not Path(golden_path).exists():
        return {}
    m = {}
    for line in open(golden_path, encoding="utf-8"):
        line = line.strip()
        if line:
            r = json.loads(line)
            m[r.get("script_id", "")] = r
    return m


def batch(trace_dir: str, output_dir: str, golden_path: str = None):
    gm = load_golden_map(golden_path) if golden_path else {}
    files = sorted(glob.glob(f"{trace_dir}/trace_*.json"))
    for f in files:
        name = Path(f).stem
        trace_to_md(f, f"{output_dir}/{name}.md", golden_map=gm)
        print(f"{name} → {output_dir}/{name}.md")
    print(f"\n共 {len(files)} 条 → {output_dir}/")


def main():
    cfg = get_config()
    ap = argparse.ArgumentParser(description="trace_to_markdown — 刷新版(messages, bundle)")
    ap.add_argument("--trace", help="单条 trace json")
    ap.add_argument("--trace-dir", help="批量目录", default=str(cfg.paths.traces))
    ap.add_argument("--output", help="单条输出路径")
    ap.add_argument("--output-dir", default=str(cfg.paths.root / "data" / "md"), help="批量输出目录")
    ap.add_argument("--golden", default=str(cfg.paths.golden / "golden_output.jsonl"), help="golden_output.jsonl(注入判定)")
    args = ap.parse_args()
    if args.trace:
        gm = load_golden_map(args.golden) if args.golden else {}
        trace_to_md(args.trace, args.output, golden_map=gm)
        print(f"已保存: {args.output}")
    elif args.trace_dir:
        batch(args.trace_dir, args.output_dir, args.golden)
    else:
        print("请指定 --trace 或 --trace-dir")


if __name__ == "__main__":
    main()
