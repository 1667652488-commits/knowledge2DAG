#!/usr/bin/env python3
"""
agent_client.py —— agent/adapter 网关, 端点来自 common.config。

搬自 V3 chat_with_agent, BASE_URL/AGENT_NAME 改为 config(env 优先), 不再硬编码。
暴露 invoke / get_cleaned_traces / call_agent / skill_list / skill_content /
update_skill / restore_skill, 与 V3 签名兼容。
"""
from __future__ import annotations

import json
import time
import uuid

import requests

from .config import get_config


def _cfg():
    return get_config().agent


def invoke(query: str, conversation_id: str = None,
           agent_name: str = None, timeout: int = None) -> dict:
    """触发 agent 对话(服务器消费 SSE, 返回成功/失败摘要)。"""
    c = _cfg()
    cid = conversation_id or str(uuid.uuid4())
    ag = agent_name or c.agent_name
    url = f"{c.base_url}/api/v1/agents/{ag}/conversations/{cid}"
    try:
        resp = requests.post(url, json={"query": query}, timeout=timeout or c.timeout_invoke)
    except Exception as exc:
        return {"conversation_id": cid, "success": False, "error": str(exc), "raw": None}
    if resp.status_code != 200:
        return {"conversation_id": cid, "success": False,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}", "raw": None}
    payload = resp.json()
    return {"conversation_id": cid, "success": payload.get("success"),
            "error": payload.get("error"), "raw": payload}


def get_cleaned_traces(conversation_id: str, agent_name: str = None,
                       timeout: int = None) -> dict:
    """取完整清洗 trace(messages 列表, 含 ask_user 中断等全量事件)。"""
    c = _cfg()
    ag = agent_name or c.agent_name
    url = f"{c.base_url}/api/v1/agents/{ag}/cleaned-traces/{conversation_id}"
    try:
        resp = requests.get(url, timeout=timeout or c.timeout_read)
    except Exception as exc:
        return {"error": str(exc), "messages": []}
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "messages": []}
    return resp.json()


def call_agent(query: str, conversation_id: str = None,
               agent_name: str = None) -> dict:
    """端到端: invoke → 等落盘 → get_cleaned_traces → 返回 trace。"""
    c = _cfg()
    inv = invoke(query, conversation_id, agent_name)
    if not inv.get("success"):
        return {"conversation_id": inv["conversation_id"], "error": inv.get("error"),
                "trace": None, "invoke_result": inv}
    if c.trace_wait:
        time.sleep(c.trace_wait)
    trace = get_cleaned_traces(inv["conversation_id"], agent_name)
    return {"conversation_id": inv["conversation_id"],
            "error": None if trace.get("messages") else "cleaned-traces 空",
            "trace": trace, "invoke_result": inv}


# ── Skill 管理 ────────────────────────────────────────────────

def _skill_post(action: str, skill_name: str = None, content: str = None,
                agent_name: str = None, timeout: int = None) -> dict:
    c = _cfg()
    body = {"agent_name": agent_name or c.agent_name, "action": action}
    if skill_name is not None:
        body["skill_name"] = skill_name
    if content is not None:
        body["content"] = content
    url = f"{c.base_url}/api/v1/skills"
    resp = requests.post(url, json=body, timeout=timeout or c.timeout_skill)
    return resp.json() if resp.status_code == 200 else {"error": f"HTTP {resp.status_code}"}


def skill_list(agent_name: str = None, timeout: int = None) -> dict:
    return _skill_post("skill_list", agent_name=agent_name, timeout=timeout)


def skill_content(skill_name: str, agent_name: str = None, timeout: int = None) -> dict:
    return _skill_post("skill_content", skill_name=skill_name,
                       agent_name=agent_name, timeout=timeout)


def update_skill(skill_name: str, content: str, agent_name: str = None,
                 timeout: int = None) -> dict:
    return _skill_post("update_skill", skill_name=skill_name, content=content,
                       agent_name=agent_name, timeout=timeout)


def restore_skill(skill_name: str, agent_name: str = None, timeout: int = None) -> dict:
    return _skill_post("restore_skill", skill_name=skill_name,
                       agent_name=agent_name, timeout=timeout)


if __name__ == "__main__":
    c = _cfg()
    print(f"base_url={c.base_url} agent={c.agent_name}")
    r = call_agent("查询小米科技有限公司的客户分层分类信息")
    print(f"cid={r['conversation_id']} error={r['error']}")
    if r.get("trace"):
        msgs = r["trace"].get("messages", [])
        print(f"messages 数: {len(msgs)}")
