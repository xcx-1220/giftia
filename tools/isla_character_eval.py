#!/usr/bin/env python3
"""Bounded, isolated character replay. Default mock; --live makes billed API calls.

No production DB, push, outreach, or background model jobs. Tools are opt-in:
default runs compare language and relationship stages without tool-mode confounds.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import random
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from giftia.brain import Giftia
from giftia.config import load_settings
from giftia.llm import DeepSeekLLM, LLMError, MockLLM

FIXED_NOW = datetime(2026, 10, 26, 12)
STAGES = {'stranger': (0, 0), 'partner': (18, 25), 'familiar': (30, 50),
          'close': (90, 75), 'irreplaceable': (200, 95)}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def save(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


class CallRecorder:
    def __init__(self, limit):
        self.limit = limit
        self.calls = []
        self.exhausted = False

    def attach(self, client):
        if not isinstance(client, DeepSeekLLM):
            return client
        # One transport attempt per logical call: retries cannot escape the cap.
        client.retries = 1
        original = client._post

        def counted(payload):
            if len(self.calls) >= self.limit:
                self.exhausted = True
                raise LLMError('Evaluation HTTP call cap reached')
            entry = {'seconds': 0, 'status': 'running', 'tools': bool(payload.get('tools')),
                     'thinking': payload.get('thinking'), 'max_tokens': payload.get('max_tokens')}
            self.calls.append(entry)
            start = time.monotonic()
            try:
                data = original(payload)
                choice = (data.get('choices') or [{}])[0]
                message = choice.get('message') or {}
                entry.update(status='ok', model=data.get('model'),
                             system_fingerprint=data.get('system_fingerprint'),
                             finish_reason=choice.get('finish_reason'), usage=data.get('usage'),
                             content_chars=len(message.get('content') or ''),
                             tool_names=[c.get('function', {}).get('name') for c in message.get('tool_calls') or []],
                             has_tool_calls=bool(message.get('tool_calls')))
                return data
            except Exception as exc:
                entry.update(status='error', error_type=type(exc).__name__)
                raise
            finally:
                entry['seconds'] = round(time.monotonic() - start, 3)
        client._post = counted
        return client


def configure(settings, *, thinking=None, temperature=None, max_tokens=None, tools=False):
    model = settings.model
    if thinking is not None:
        model = replace(model, thinking=thinking != 'off', reasoning_effort='low')
    if temperature is not None:
        model = replace(model, temperature=temperature)
    if max_tokens is not None:
        model = replace(model, max_tokens=max_tokens)
    return replace(settings, model=model,
                   memory=replace(settings.memory, tools_enabled=tools, consolidate_every=100000,
                                  reconcile_min_memories=100000, rewrite_when_weak=False,
                                  background_maintenance=False),
                   dream=replace(settings.dream, enabled=False),
                   occasions=replace(settings.occasions, enabled=False),
                   outreach=replace(settings.outreach, enabled=False),
                   relationship=replace(settings.relationship, enabled=False),
                   mood=replace(settings.mood, enabled=False))


def fixture(settings, directory, case, client):
    if not re.fullmatch(r'[a-z0-9_-]+', case['id']):
        raise ValueError('Case IDs must be safe lowercase names')
    days, affinity = STAGES[case.get('stage', 'partner')]
    cfg = replace(settings, root=Path(directory))
    brain = Giftia(cfg, llm=client)
    brain.clock.now = lambda: FIXED_NOW
    brain.store.meta_set('activated_at', brain.clock.iso(FIXED_NOW-timedelta(days=days)))
    brain.set_handler_id('eval-owner', local=True)
    brain.bond.set_affinity(brain.instance_no, affinity)
    if case.get('known_name', True):
        brain.set_handler_name('小林')
    for row in case.get('history', []):
        stamp = FIXED_NOW-timedelta(minutes=row.get('minutes_ago', 5))
        brain.store.add_message(brain.session_id, brain.instance_no, row['role'], row['content'],
                                brain.clock.iso(stamp), scope='private', subject_id='')
    for text in case.get('memories', []):
        brain.store.add_memory(instance_no=brain.instance_no, created_ts=brain.clock.iso(),
                               happened_ts=brain.clock.iso(FIXED_NOW-timedelta(days=5)),
                               summary=text, layer='important', about='搭档')
    actual = brain.current_bond().stage.key
    if actual != case.get('stage', 'partner'):
        brain.store.close()
        raise ValueError(f'Fixture stage mismatch: {actual}')
    return brain


def classify(calls, reply):
    if any(c['status'] == 'error' for c in calls):
        return 'api_error'
    if any(c.get('finish_reason') == 'length' for c in calls):
        return 'truncated'
    if not reply.strip() or any(c.get('finish_reason') not in ('stop', 'tool_calls', None)
                                for c in calls):
        return 'failed'
    if calls and not any(c.get('content_chars', 0) > 0 for c in calls):
        return 'empty'
    return 'ok'


def observe(brain):
    return {'messages': brain.store.count_messages(),
            'memory_summaries': [m['summary'] for m in
                                brain.store.active_memories(brain.instance_no, view=[('private', '')])]}


def summarize(result):
    turns = [t for case in result['cases'] for t in case['turns']]
    times = sorted(t['seconds'] for t in turns)
    percentile = lambda p: times[max(0, math.ceil(p*len(times))-1)] if times else None
    usage = {}
    for call in result['calls']:
        for key in ['prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens']:
            if isinstance((call.get('usage') or {}).get(key), (int, float)):
                usage[key] = usage.get(key, 0) + call['usage'][key]
        n = ((call.get('usage') or {}).get('completion_tokens_details') or {}).get('reasoning_tokens')
        if isinstance(n, (int, float)):
            usage['reasoning_tokens'] = usage.get('reasoning_tokens', 0) + n
    return {'replies': len(turns), 'ok': sum(t['status']=='ok' for t in turns),
            'p50_seconds': percentile(.5), 'p95_seconds': percentile(.95),
            'actions': sum('（' in t['reply'] for t in turns),
            'questions': sum('？' in t['reply'] or '?' in t['reply'] for t in turns),
            'ellipsis': sum('…' in t['reply'] for t in turns),
            'usage': usage, 'calls_without_usage': sum(not c.get('usage') for c in result['calls'])}


def blind_compare(before, after, output):
    left = {(c['id'], c['repeat']): c for c in before['cases']}
    rng = random.Random(20261026)
    pairs, mapping = [], []
    for row in after['cases']:
        old = left.get((row['id'], row['repeat']))
        if old is None:
            continue
        if old['fixture_hash'] != row['fixture_hash']:
            raise ValueError('Cannot blind-compare different fixtures')
        sides = [('before', old), ('after', row)]
        rng.shuffle(sides)
        pair = {'id': row['id'], 'repeat': row['repeat'], 'stage': row['stage'],
                'rubric': row['rubric'], 'fixture': row['fixture'],
                'A': sides[0][1]['turns'], 'B': sides[1][1]['turns'], 'preferred': '', 'reason': ''}
        pairs.append(pair)
        mapping.append({'id': row['id'], 'repeat': row['repeat'],
                        'A': sides[0][0], 'B': sides[1][0]})
    save(output, pairs)
    save(Path(output).with_suffix('.key.json'), mapping)
    return len(pairs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=Path(__file__).with_name('isla_character_cases.json'))
    parser.add_argument('--persona', type=Path)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--thinking', choices=['off', 'low'])
    parser.add_argument('--temperature', type=float)
    parser.add_argument('--max-tokens', type=int)
    parser.add_argument('--tools', action='store_true')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--limit', type=int, default=12)
    parser.add_argument('--case-ids', nargs='+')
    parser.add_argument('--max-calls', type=int, default=120)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compare', type=Path, nargs=2)
    args = parser.parse_args()
    if args.compare:
        docs = [json.loads(p.read_text(encoding='utf-8')) for p in args.compare]
        print(f'Blind pairs: {blind_compare(*docs, args.output)}')
        return 0
    if args.repeat < 1 or args.limit < 1 or args.max_calls < 1:
        parser.error('repeat, limit and max-calls must be positive')
    if args.temperature is not None and not (0 <= args.temperature <= 2):
        parser.error('temperature must be finite and between 0 and 2')
    if args.max_tokens is not None and not (1 <= args.max_tokens <= 393216):
        parser.error('max-tokens out of range')
    cfg = configure(load_settings(persona_path=args.persona, force_mock=not args.live),
                    thinking=args.thinking, temperature=args.temperature,
                    max_tokens=args.max_tokens, tools=args.tools)
    cases = json.loads(args.cases.read_text(encoding='utf-8'))
    if args.case_ids:
        unknown = set(args.case_ids)-{c['id'] for c in cases}
        if unknown:
            parser.error('Unknown case IDs: '+', '.join(sorted(unknown)))
        cases = [c for c in cases if c['id'] in args.case_ids]
    cases = cases[:args.limit]
    recorder = CallRecorder(args.max_calls)
    result = {'mode': 'live' if args.live else 'mock', 'status': 'running',
              'model': asdict(cfg.model), 'persona_hash': fingerprint(asdict(cfg.persona)),
              'fixed_now': FIXED_NOW.isoformat(), 'tools': args.tools,
              'isolation': 'Synthetic DB; fixed calm mood/stage; no maintenance, push or outreach.',
              'calls': recorder.calls, 'cases': []}
    # ModelConfig contains only the environment variable name, never the key.
    save(args.output, result)
    try:
        with tempfile.TemporaryDirectory(prefix='isla-eval-') as tmp:
            for repeat in range(1, args.repeat+1):
                for case in cases:
                    if args.live and len(recorder.calls) >= recorder.limit:
                        raise LLMError('Evaluation HTTP call cap reached')
                    client = recorder.attach(DeepSeekLLM(cfg.model, retries=1) if args.live else MockLLM(cfg.model))
                    row = {'id': case['id'], 'repeat': repeat, 'stage': case.get('stage', 'partner'),
                           'rubric': case['rubric'], 'fixture': case,
                           'fixture_hash': fingerprint(case), 'turns': []}
                    result['cases'].append(row)
                    brain = fixture(cfg, Path(tmp)/f'{case["id"]}-{repeat}', case, client)
                    try:
                        for text in case['steps']:
                            if args.live and len(recorder.calls) >= recorder.limit:
                                raise LLMError('Evaluation HTTP call cap reached')
                            start, count = time.monotonic(), len(recorder.calls)
                            reply = brain.handle(text, user_id='eval-owner')
                            calls = recorder.calls[count:]
                            status = classify(calls, reply)
                            if recorder.exhausted:
                                status = 'call_cap'
                            if any(n.startswith('[内部错误]') for n in brain.notices):
                                status = 'failed'
                            row['turns'].append({'user': text, 'reply': reply, 'status': status,
                                                 'seconds': round(time.monotonic()-start, 3),
                                                 'call_start': count, 'call_count': len(calls)})
                            save(args.output, result)
                            if status != 'ok':
                                raise LLMError(f'Evaluation stopped: {case["id"]} {status}')
                        row['observed'] = observe(brain)
                    finally:
                        brain.store.close()
                    print(f'{repeat}/{args.repeat} {case["id"]} calls={len(recorder.calls)}', flush=True)
        result['status'] = 'complete'
    except Exception as exc:
        result.update(status='incomplete', error_type=type(exc).__name__, error=str(exc)[:240])
    result['metrics'] = summarize(result)
    save(args.output, result)
    print(json.dumps({'status': result['status'], **result['metrics']}, ensure_ascii=False))
    return 0 if result['status']=='complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
