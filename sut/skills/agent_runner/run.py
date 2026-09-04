#!/usr/bin/env python3
"""
agent_runner — 跑批产 messages trace(invoke + cleaned-traces)。

链路起点: 剧本 → 逐轮 invoke agent → cleaned-traces → 存 messages trace。
--with-golden: 顺带调 golden_gen 逐条判 expected_behavior+result(GU 需 cached 或现生成)。

用法:
    python -m skills.agent_runner.run --scripts data/scripts/badcase_scripts.json
    python -m skills.agent_runner.run --scripts data/scripts/badcase_scripts.json --with-golden
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

_BUNDLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BUNDLE))

from common import agent_client  # noqa: E402
from common.config import get_config  # noqa: E402


def _wait_turn_ready(conv_id, poll_timeout, stable_window, poll_interval) -> bool:
    """等当前 turn 产生 ask_user 中断(awaiting_user_response) 或 messages 稳定(直接回复无中断)。

    invoke 是 fire-and-forget, agent 异步处理; 下一轮必须等到 agent 真在 ask_user 等(中断)
    或已最终回复(稳定)再发, 否则下一轮发太早没答上 ask_user。返回是否就绪。
    """
    t0 = time.time()
    last_n = -1
    stable = 0
    stable_need = max(1, stable_window // poll_interval)
    while time.time() - t0 < poll_timeout:
        try:
            ms = agent_client.get_cleaned_traces(conv_id).get("messages") or []
        except Exception:
            ms = []
        # ask_user 中断就绪(tool 结果含 awaiting_user_response)
        if "awaiting_user_response" in json.dumps(ms, ensure_ascii=False):
            print(f"  → ask_user 中断就绪 (等了 {time.time()-t0:.0f}s), 发下一轮当答案")
            return True
        n = len(ms)
        if n == last_n:
            stable += 1
        else:
            stable = 0
            last_n = n
        if stable >= stable_need:
            print(f"  → turn 稳定无中断 ({n}msg, 等了 {time.time()-t0:.0f}s), 发下一轮当新 query")
            return True
        print(f"  …等中断/稳定 msgs={n} ({time.time()-t0:.0f}s/{poll_timeout}s)", flush=True)
        time.sleep(poll_interval)
    return False


def run_batch(scripts_path, output_dir, with_golden=False, gu_path=None,
              skill_dir=None, only_ids=None, poll_timeout=600, poll_interval=5,
              stable_window=45):
    scripts = json.loads(Path(scripts_path).read_text(encoding="utf-8"))
    if only_ids:
        scripts = [s for s in scripts if s.get("id") in only_ids]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(output_dir) / ts
    traces_dir = run_dir / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    # golden 内联判定(--with-golden)
    golden_fn = None
    gu_text = ""
    golden_fp = None
    if with_golden:
        from skills.golden_gen.run import generate_golden
        if not gu_path or not Path(gu_path).exists():
            print("✗ --with-golden 需 cached global_understanding(--gu-path); 先跑 golden_gen 批量生成")
            return
        gu_text = Path(gu_path).read_text(encoding="utf-8")
        golden_dir = run_dir / "golden"
        golden_dir.mkdir(parents=True, exist_ok=True)
        golden_fp = open(golden_dir / "golden_output.jsonl", "w", encoding="utf-8")
        golden_fn = generate_golden
        print(f"  复用 cached GU: {gu_path}")

    results = []
    for i, script in enumerate(scripts, 1):
        sid = script.get("id", f"script_{i}")
        turns = script.get("fixed_turns", [])
        conv_id = str(uuid.uuid4())
        print(f"\n[{i}/{len(scripts)}] {sid} | turns={len(turns)} | {turns[0][:50] if turns else '?'}...")

        errors = []
        for turn_idx, query in enumerate(turns, 1):
            print(f"  turn{turn_idx}: {query[:60]}...")
            inv = agent_client.invoke(query, conv_id)
            if not inv.get("success"):
                errors.append(f"turn{turn_idx} invoke失败: {inv.get('error')}")
                break
            # invoke 是 fire-and-forget(5ms); 非末轮: 必须等当前轮产生 ask_user 中断
            # (awaiting_user_response) 或 turn 稳定(直接回复无中断) 再发下一轮——否则下一轮
            # 发太早没答上 ask_user, agent 卡在中断等待, trace 半截
            if turn_idx < len(turns):
                if not _wait_turn_ready(conv_id, poll_timeout, stable_window, poll_interval):
                    print(f"  turn{turn_idx}: 等 {poll_timeout}s 未就绪(无中断未稳定), 强发下一轮")

        msgs = []
        if not errors:
            # 轮询 cleaned-traces: agent 异步(规划 LLM 可能跑几百秒), 等 messages 稳定(连续 N 次不涨=跑完)或 timeout
            # cleaned JSON 在 trace 变大后可能解析失败(content 非法字符), 容错: 解析失败则保留上次, 继续轮询
            t0 = time.time()
            last_count = -1
            stable = 0
            stable_need = max(1, stable_window // poll_interval)  # 连续多少次不涨视为完成
            while time.time() - t0 < poll_timeout:
                try:
                    trace = agent_client.get_cleaned_traces(conv_id)
                    cand = trace.get("messages", []) or []
                except Exception as ce:
                    cand = msgs  # JSON 解析失败, 保留上次, 视为不变
                elapsed = time.time() - t0
                if len(cand) > last_count:
                    last_count = len(cand)
                    stable = 0
                    msgs = cand
                else:
                    stable += 1
                if last_count > 0 and stable >= stable_need:
                    print(f"  cleaned-traces: {last_count} 条 messages (等了 {elapsed:.0f}s, 稳定 {stable}次)")
                    break
                print(f"  …messages={last_count} (等 {elapsed:.0f}s/{poll_timeout}s, stable={stable}/{stable_need})", flush=True)
                time.sleep(poll_interval)
            else:
                # timeout
                if msgs:
                    print(f"  cleaned-traces: {len(msgs)} 条 (轮询 {poll_timeout}s 超时, 取当前; agent 可能没跑完, 查 raw traces)")
                else:
                    errors.append(f"cleaned-traces 空 (轮询 {poll_timeout}s 超时)")

        trace_data = {"script_id": sid, "conversation_id": conv_id,
                      "script": script, "messages": msgs, "errors": errors}
        trace_file = traces_dir / f"trace_{sid}.json"
        trace_file.write_text(json.dumps(trace_data, ensure_ascii=False, indent=2), encoding="utf-8")

        golden_rec = None
        if with_golden and not errors and golden_fn:
            golden_rec = golden_fn(trace_data, gu_text, skill_dir=skill_dir)
            golden_fp.write(json.dumps(golden_rec, ensure_ascii=False) + "\n")

        rec = {"script_id": sid, "conversation_id": conv_id,
               "success": len(errors) == 0,
               "errors": "; ".join(errors) if errors else "",
               "message_count": len(msgs)}
        if golden_rec:
            rec.update({"expected_behavior": golden_rec.get("expected_behavior", ""),
                        "result": golden_rec.get("result", ""),
                        "reason": golden_rec.get("reason", "")})
        results.append(rec)
        print(f"  → {'✓' if not errors else '✗ ' + '; '.join(errors)}")

    csv_path = run_dir / "batch_result.csv"
    fields = ["script_id", "conversation_id", "success", "errors", "message_count"]
    if with_golden:
        fields += ["expected_behavior", "result", "reason"]
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in fields})
    if golden_fp:
        golden_fp.close()

    ok = sum(1 for r in results if r["success"])
    print(f"\n{'='*60}\n共 {len(results)} 条 | 成功 {ok} | 失败 {len(results)-ok}")
    print(f"traces: {traces_dir}/\nCSV: {csv_path}\nrun: {run_dir}/")


def main():
    cfg = get_config()
    ap = argparse.ArgumentParser(description="agent_runner — 跑批产 messages trace")
    ap.add_argument("--scripts", default=str(cfg.paths.root / "data" / "scripts" / "badcase_scripts.json"))
    ap.add_argument("--output-dir", default=str(cfg.paths.runs / "agent"))
    ap.add_argument("--with-golden", action="store_true")
    ap.add_argument("--gu-path", default=str(cfg.paths.golden / "global_understanding.txt"))
    ap.add_argument("--skill-dir", default=str(cfg.paths.skills_flat))
    ap.add_argument("--ids", default=None, help="只跑指定 id(逗号分隔)")
    ap.add_argument("--poll-timeout", type=int, default=600,
                    help="单条剧本轮询 cleaned-traces 超时秒(默认 600; agent 异步, 规划 LLM 可能跑几百秒)")
    ap.add_argument("--poll-interval", type=int, default=5, help="轮询间隔秒(默认 5)")
    ap.add_argument("--stable-window", type=int, default=45,
                    help="messages 连续不涨多少秒视为 agent 跑完(默认 45; 推理长暂停可调大)")
    args = ap.parse_args()
    only_ids = args.ids.split(",") if args.ids else None
    run_batch(args.scripts, args.output_dir, with_golden=args.with_golden,
              gu_path=args.gu_path, skill_dir=args.skill_dir, only_ids=only_ids,
              poll_timeout=args.poll_timeout, poll_interval=args.poll_interval,
              stable_window=args.stable_window)


if __name__ == "__main__":
    main()
