import json
from pathlib import Path

run_dir = Path('runs/20260903_105240_strong_skilldriven_baseline_full/traces')
golden = {}
for line in open('sut/runs/golden/20260903_151633/golden_output.jsonl', encoding='utf-8'):
    d = json.loads(line)
    golden[d['id']] = d

def show(trace_file):
    t = json.load(open(run_dir / trace_file, encoding='utf-8'))
    g = next(v for k, v in golden.items() if k == t['conversation_id'])
    print('=' * 70)
    print(f"{trace_file} | script: {t['script']['instruction'][:110]}")
    calls = [tc['name'] for m in t['messages'] for tc in m.get('tool_calls', [])]
    print('工具序列:', ' -> '.join(calls))
    users = [m['content'][:80] for m in t['messages'] if m['_class'] == 'UserMessage']
    print('用户首条:', users[0] if users else '')
    print('golden EB:', g['expected_behavior'][:180])
    print('golden reason:', g['reason'][:250])

for f in ['trace_2_t0.json', 'trace_0_t0.json', 'trace_38_t2.json']:
    show(f)
