#!/usr/bin/env python3
"""
state.py —— 运行中间状态持久化(断点续跑/审计)。

runs 根目录取自 common.config, 不再硬编码 d:/...。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import get_config


def get_run_dir(skill_name: str, run_id: str | None = None) -> Path:
    run_id = run_id or time.strftime("%Y%m%d_%H%M%S")
    run_dir = get_config().paths.runs / skill_name / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def save(skill_name: str, name: str, data: Any, run_id: str | None = None) -> Path:
    run_dir = get_run_dir(skill_name, run_id)
    path = run_dir / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load(skill_name: str, name: str, run_id: str) -> Any:
    path = get_config().paths.runs / skill_name / run_id / f"{name}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_text(skill_name: str, name: str, content: str,
              run_id: str | None = None, suffix: str = ".txt") -> Path:
    run_dir = get_run_dir(skill_name, run_id)
    path = run_dir / f"{name}{suffix}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def load_text(skill_name: str, name: str, run_id: str, suffix: str = ".txt") -> str:
    path = get_config().paths.runs / skill_name / run_id / f"{name}{suffix}"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def list_runs(skill_name: str) -> list[str]:
    root = get_config().paths.runs / skill_name
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())
