#!/usr/bin/env python3
"""
managed_docs.py —— 经 adapter /api/v1/managed-docs 读 agent 的托管文档(AgentRule 等)。

契约(见记忆 [[zdt-adapter-8900-contract]]):
  POST {base_url}/api/v1/managed-docs  body={"action":"content","agent_name","doc_kind"}
  → {"doc_kind": ..., "content": "..."}
  此端点走 agent 配置级(managed_docs.path), **不经 sandbox**, edp_agent 多 sandbox 二义时仍可用。

用法(模块):
  from common.managed_docs import fetch_managed_doc, save_managed_doc
  content = fetch_managed_doc("agent_rule")                 # 取原文
  save_managed_doc("agent_rule", "data/skills_cache/AgentRule.md")  # 取 + 落盘

用法(CLI):
  python -m common.managed_docs agent_rule                                   # 打印 content
  python -m common.managed_docs agent_rule --out data/skills_cache/AgentRule.md
  python -m common.managed_docs agent_rule --agent-name edp_agent --out X.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

from .config import get_config


def fetch_managed_doc(doc_kind: str, agent_name: str = None,
                      timeout: int = None) -> str | None:
    """读 agent 托管文档原文。失败返回 None(不抛, 调用方自行降级)。"""
    c = get_config().agent
    ag = agent_name or c.agent_name
    url = f"{c.base_url}/api/v1/managed-docs"
    body = {"action": "content", "agent_name": ag, "doc_kind": doc_kind}
    try:
        resp = requests.post(url, json=body, timeout=timeout or c.timeout_skill)
    except Exception as exc:
        print(f"  ⚠ managed-docs[{doc_kind}] 请求异常: {exc}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"  ⚠ managed-docs[{doc_kind}] HTTP {resp.status_code}: {resp.text[:200]}",
              file=sys.stderr)
        return None
    try:
        payload = resp.json()
    except Exception:
        print(f"  ⚠ managed-docs[{doc_kind}] 响应非 JSON", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        print(f"  ⚠ managed-docs[{doc_kind}] 报错: {msg}", file=sys.stderr)
        return None
    content = payload.get("content")
    return content if isinstance(content, str) and content else None


def save_managed_doc(doc_kind: str, out_path: str | Path,
                     agent_name: str = None, timeout: int = None) -> Path | None:
    """读 agent 托管文档并落盘(父目录自动建)。失败返回 None。"""
    content = fetch_managed_doc(doc_kind, agent_name=agent_name, timeout=timeout)
    if content is None:
        return None
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    print(f"  ✓ managed-docs[{doc_kind}] 已存: {out} ({len(content)} 字)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="经 adapter 读 agent 托管文档(AgentRule 等)")
    ap.add_argument("doc_kind", help="文档类型, 如 agent_rule")
    ap.add_argument("--agent-name", default=None, help="agent 名(默认 config.agent.agent_name)")
    ap.add_argument("--out", default=None, help="落盘路径; 不传则只打印 content")
    ap.add_argument("--timeout", type=int, default=None)
    args = ap.parse_args()

    if args.out:
        p = save_managed_doc(args.doc_kind, args.out,
                             agent_name=args.agent_name, timeout=args.timeout)
        return 0 if p else 1
    content = fetch_managed_doc(args.doc_kind, agent_name=args.agent_name,
                                timeout=args.timeout)
    if content is None:
        return 1
    print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
