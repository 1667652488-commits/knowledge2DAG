# -*- coding: utf-8 -*-
"""对拍①新版误杀归因：golden 判失败但硬判通过的 case，分类 golden 的理由 + 校验认证真伪。"""
import json
import sys
from collections import Counter
from pathlib import Path

NEW = sys.argv[1] if len(sys.argv) > 1 else 'sut/runs/golden/20260912_100559/golden_output.jsonl'
RUN = sys.argv[2] if len(sys.argv) > 2 else 'runs/20260903_105240_strong_skilldriven_baseline_full'

golden = {}
for line in open(NEW, encoding='utf-8'):
    d = json.loads(line)
    golden[d['id']] = d

run_dir = Path(RUN)
cid2tf = {}
for tf in (run_dir / 'traces').glob('trace_*.json'):
    t = json.loads(tf.read_text(encoding='utf-8'))
    cid2tf[t['conversation_id']] = tf.name

rewards = json.loads((run_dir / 'tau_reward.json').read_text(encoding='utf-8'))
cases = []
for cid, row in golden.items():
    tf = cid2tf.get(cid)
    if not tf:
        continue
    t = json.load(open(run_dir / 'traces' / tf, encoding='utf-8'))
    key = f"{t['script_id']}_t{tf.split('_t')[-1].replace('.json', '')}"
    if rewards.get(key, {}).get('reward') == 1.0 and row['result'] == '失败':
        tools = [tc['name'] for m in t['messages'] for tc in m.get('tool_calls', [])]
        cases.append({'trace': tf, 'reason': row.get('reason', ''),
                      'auth_called': any(x.startswith('find_user_id_by') for x in tools)})

print('新版误杀总数:', len(cases))
cat = Counter()
for c in cases:
    r = c['reason']
    if any(w in r for w in ['越界', '售前', '拒答']):
        cat['幻觉规则(越界/拒答类)'] += 1
    elif any(w in r for w in ['认证', '身份']):
        cat['漏认证'] += 1
    elif '确认' in r:
        cat['漏用户确认'] += 1
    elif any(w in r for w in ['谎称', '伪造', '虚假', '编造']):
        cat['伪造结果'] += 1
    else:
        cat['其他'] += 1
for k, v in cat.most_common():
    print(f'{v:3d}  {k}')

auth_claimed = [c for c in cases if any(w in c['reason'] for w in ['认证', '身份'])]
real = sum(1 for c in auth_claimed if not c['auth_called'])
print(f'\n漏认证类 {len(auth_claimed)} 条: 轨迹确实未认证 {real} 条(golden抓对), 误判 {len(auth_claimed)-real} 条')

print('\n--- 幻觉规则类样例(若还有) ---')
for c in [c for c in cases if any(w in c['reason'] for w in ['越界', '售前', '拒答'])][:5]:
    print(f"[{c['trace']}] {c['reason'][:130]}")
print('\n--- 其他类样例 ---')
others = [c for c in cases if not any(w in c['reason'] for w in ['越界', '售前', '拒答', '认证', '身份', '确认', '谎称', '伪造', '虚假', '编造'])]
for c in others[:8]:
    print(f"[{c['trace']}] {c['reason'][:130]}")
