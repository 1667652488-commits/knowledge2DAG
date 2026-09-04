#!/usr/bin/env python3
"""
trace_io.py —— messages 格式 trace 的统一读写/解析(只认新 messages 格式)。

老 history/agent_result 格式已废弃。所有 phase/golden/rules 通过本模块读 trace。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List


# ── 加载/保存 ────────────────────────────────────────────────

def load_trace(path: str | Path) -> dict:
    """加载单条 trace json。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_traces(trace_dir: str | Path, glob: str = "trace_*.json") -> List[dict]:
    """加载目录下所有 trace_*.json。"""
    traces = []
    for p in sorted(Path(trace_dir).glob(glob)):
        try:
            traces.append(load_trace(p))
        except Exception:
            continue
    return traces


def save_trace(path: str | Path, trace: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2)


def get_messages(trace: dict) -> list:
    """取 trace 的 messages 列表(只认 messages, 不再回退 history)。"""
    return trace.get("messages") or []


# ── 文本抽取 ─────────────────────────────────────────────────

def get_user_text(msgs: list) -> str:
    return "\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "user")


def get_assistant_text(msgs: list) -> str:
    return "\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "assistant")


def get_last_assistant(msgs: list) -> str:
    asst = [str(m.get("content", "")) for m in msgs if m.get("role") == "assistant"]
    return asst[-1] if asst else ""


def get_tool_text(msgs: list) -> str:
    """tool 消息内容(业务报告/工具返回)。"""
    return "\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "tool")


def get_reasoning(msgs: list) -> str:
    """assistant 的 reasoning_content(=旧 think)。golden 按设计默认不注入。"""
    return "\n".join(str(m.get("reasoning_content", "")) for m in msgs
                     if m.get("role") == "assistant" and m.get("reasoning_content"))


# ── 工具调用抽取 ──────────────────────────────────────────────

def extract_tool_calls(message: dict) -> list:
    """从 assistant message 的 tool_calls(OpenAI 风格) 抽工具调用。

    返回 [{plugin, intent, desc, script_params, command, question}, ...]。
    """
    if not isinstance(message, dict) or "tool_calls" not in message:
        return []
    calls = []
    for t in (message.get("tool_calls") or []):
        if not isinstance(t, dict):
            continue
        fn = t.get("function", {})
        name = fn.get("name", "")
        args_str = fn.get("arguments", "")
        try:
            args = json.loads(args_str) if args_str else {}
        except Exception:
            args = {}
        calls.append({
            "plugin": name,
            "intent": args.get("query_intent", ""),
            "desc": args.get("query_description", ""),
            "script_params": args.get("script_params", ""),
            "command": args.get("command", ""),
            "question": args.get("question", ""),
        })
    return calls


def get_all_tool_calls(msgs: list) -> list:
    """所有 assistant 消息的 tool_calls 合并。"""
    out = []
    for m in msgs:
        if m.get("role") == "assistant":
            out.extend(extract_tool_calls(m))
    return out


def _extract_script_params(command: str) -> str:
    """从 execute_cmd 的 command 提取 SCRIPT_PARAMS 值(通用传参方式)。"""
    if not command:
        return ""
    m = re.search(r"SCRIPT_PARAMS=(['\"])(.*?)\1\s+python", command)
    if m:
        return m.group(2)
    idx = command.find("SCRIPT_PARAMS=")
    if idx != -1:
        return command[idx + 14:][:600]
    return ""


def format_tool_calls(calls: list, desc_cap: int = 60, params_cap: int = 600) -> str:
    """紧凑一行: call_versatile 显 plugin|intent|desc; ask_user 显 question; execute_cmd 显脚本参数。"""
    if not calls:
        return ""
    parts = []
    for c in calls:
        s = c.get("plugin", "")
        if c.get("intent"):
            s += f"|{c['intent']}"
        if c.get("desc"):
            s += f"|{c['desc'][:desc_cap]}"
        elif c.get("question"):
            s += f"|question:{c['question'][:desc_cap]}"
        elif c.get("script_params"):
            s += f"|脚本参数:{c['script_params'][:params_cap]}"
        elif c.get("command"):
            sp = _extract_script_params(c["command"])
            s += f"|脚本参数:{(sp or c['command'])[:params_cap]}"
        parts.append(s)
    return " ; ".join(parts)


def _truncate(text: str, cap: int) -> str:
    return text[:cap] + ("..." if len(text) > cap else "")


# ── 轨迹文本格式化(phase2-6 / golden 共用) ────────────────────

def format_trajectory_text(trace: dict, content_cap: int = 500,
                           report_cap: int = 1500) -> str:
    """把单条 trace 格式化为可读文本(供 LLM 阅读)。

    messages 格式: role=user/assistant/tool, tool_calls 在 assistant, tool 结果在 tool message。
    """
    msgs = get_messages(trace)
    conv_id = trace.get("conversation_id", "unknown")
    script_id = trace.get("script_id") or (trace.get("script", {}) or {}).get("id", "")
    lines = [f"【对话ID: {conv_id} (剧本 {script_id})】"]

    seen_reports = set()
    for idx, m in enumerate(msgs):
        raw_role = m.get("role", "")
        if raw_role == "user":
            role = "顾客"
        elif raw_role == "assistant":
            role = "Agent"
        elif raw_role == "tool":
            role = "工具结果"
        else:
            role = raw_role or "?"
        content = (m.get("content") or "").strip()
        lines.append(f"  第{idx+1}轮 [{role}]: {_truncate(content, content_cap)}")

        if role == "Agent":
            tc = extract_tool_calls(m)
            if tc:
                lines.append(f"    工具调用: {format_tool_calls(tc)}")

        if role == "工具结果":
            # 业务报告去重(跨轮相同只留一次, 是某 content 子串则跳过)
            if content and content not in seen_reports:
                key = content[:200]
                if key not in seen_reports:
                    seen_reports.add(key)
                    lines.append(f"    业务报告/返回: {_truncate(content, report_cap)}")
    lines.append("")
    return "\n".join(lines)
