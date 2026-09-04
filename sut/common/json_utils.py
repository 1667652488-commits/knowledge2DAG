#!/usr/bin/env python3
"""
json_utils.py —— 健壮的 LLM JSON 提取 + 错误识别 + 重试辅助。

解决 kimi 旧版 extract_json_block 的两个硬伤:
1. LLM 在 JSON 前后加解释文字 → json.loads 直接崩, 整个 run 崩。
2. LLM 超时/连接失败时 call_llm 返回 "[LLM ...]" 错误串, 被当代码喂给解析器。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable


def is_llm_error(out: str) -> bool:
    """call_llm 失败时返回 [LLM ...] 错误串, 识别它避免当成 JSON/代码。"""
    if not out:
        return True
    return out.startswith("[LLM") or "调用超时" in out or "连接失败" in out or "HTTP 错误" in out or "调用错误" in out


def extract_json_block(text: str) -> Any:
    """从 LLM 返回文本提取 JSON: 剥围栏 → 取首个 {...}/[...] → json.loads。

    比 kimi 旧版更健壮: 容忍 JSON 前后解释文字、嵌套 ```、多余空白。
    Raises json.JSONDecodeError 若确实无合法 JSON。
    """
    if text is None:
        raise json.JSONDecodeError("空输入", "", 0)
    s = text.strip()
    # 剥最外层 markdown 围栏
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    # 直接试
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 取首个 {...} 或 [...] 块(贪心, 跨行)
    m = re.search(r"(\{.*\}|\[.*\])", s, flags=re.S)
    if m:
        return json.loads(m.group(1))
    raise json.JSONDecodeError(f"未找到 JSON 块: {s[:120]}", s, 0)


def call_llm_json(
    call_fn: Callable[..., str],
    messages: list,
    retries: int = 3,
    retry_delay: float = 2.0,
    **kwargs,
) -> Any:
    """调 LLM → 识别错误 → 重试 → 抽 JSON。失败返回 None。

    call_fn: 形如 common.llm_client.call_llm(messages, **kwargs) -> str
    """
    last_raw = ""
    for attempt in range(retries):
        raw = call_fn(messages, **kwargs)
        if is_llm_error(raw):
            time.sleep(retry_delay * (attempt + 1))
            continue
        last_raw = raw
        try:
            return extract_json_block(raw)
        except json.JSONDecodeError:
            time.sleep(retry_delay * (attempt + 1))
            continue
    return None
