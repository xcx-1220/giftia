#!/usr/bin/env python3
"""Isolated multi-turn replay. Defaults to mock; --live explicitly enables billed API calls."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import random
import sys
import tempfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,default=Path(__file__).with_name('chat_quality_cases.json'))
    parser.add_argument('--code-root',type=Path,default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--limit',type=int,default=12)
    parser.add_argument('--max-calls',type=int,default=150)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--compare',type=Path,nargs=2)
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.compare:
        before,after=[json.loads(p.read_text(encoding='utf-8')) for p in args.compare]
        by_id={r['id']:r for r in before['cases']}
        answer=[];key={};rng=random.Random(20261025)
        for row in after['cases']:
            if row['id'] not in by_id:continue
            sides=[('before',by_id[row['id']]),('after',row)];rng.shuffle(sides)
            key[row['id']]={chr(65+i):name for i,(name,_) in enumerate(sides)}
            answer.append({'id':row['id'],'rubric':row['rubric'],**{chr(65+i):item['turns'] for i,(_,item) in enumerate(sides)},
                           'preferred':'','reason':''})
        args.output.write_text(json.dumps(answer,ensure_ascii=False,indent=2),encoding='utf-8')
        args.output.with_suffix('.key.json').write_text(json.dumps(key,ensure_ascii=False,indent=2),encoding='utf-8')
        print(f'Blind comparison: {len(answer)} cases; labels saved separately.')
        return 0
    sys.path.insert(0,str(args.code_root.resolve()))
    from giftia.config import load_settings, load_env_file
    from giftia.brain import Giftia
    from giftia.llm import DeepSeekLLM, MockLLM, LLMError
    if args.env_file:load_env_file(args.env_file)
    settings=load_settings(force_mock=not args.live)
    cases=json.loads(args.cases.read_text(encoding='utf-8'))[:max(0,args.limit)]
    result={'mode':'live' if args.live else 'mock','model':settings.model.name,'temperature':settings.model.temperature,
            'thinking':settings.model.thinking,'cases':[],'network_calls':0,'started':datetime.now().isoformat()}
    def count_client(client):
        if not args.live:return client
        post=client._post
        def counted(payload):
            if result['network_calls']>=args.max_calls:raise LLMError('Evaluation network-call cap reached')
            result['network_calls']+=1
            return post(payload)
        client._post=counted
        return client
    with tempfile.TemporaryDirectory(prefix='giftia-eval-',dir=args.output.parent) as tmp:
        for case in cases:
            cfg=replace(settings,root=Path(tmp)/case['id'])
            client=count_client(DeepSeekLLM(cfg.model,retries=1) if args.live else MockLLM(cfg.model))
            brain=Giftia(cfg,llm=client)
            try:
                brain.set_handler_id('eval-owner',local=True)
                brain.clock.advance(18*24)
                brain.bond.set_affinity(brain.instance_no,34.85)
                brain.set_handler_name('小林')
                for memory in case.get('memories',[]):
                    brain.store.add_memory(instance_no=brain.instance_no,created_ts=brain.clock.iso(),happened_ts=brain.clock.iso(),
                                            summary=memory,layer='important',about='搭档')
                item={'id':case['id'],'rubric':case['rubric'],'turns':[]}
                for user in case['steps']:
                    if result['network_calls']>=args.max_calls:raise RuntimeError('Evaluation call cap reached')
                    started=datetime.now()
                    reply=brain.handle(user,user_id='eval-owner' if case.get('scene','c2c')=='c2c' else 'eval-friend',
                                       scene=case.get('scene','c2c'),is_handler=case.get('scene','c2c')=='c2c')
                    item['turns'].append({'user':user,'reply':reply,'seconds':round((datetime.now()-started).total_seconds(),3)})
                result['cases'].append(item)
                args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                print(f"{len(result['cases'])}/{len(cases)} {case['id']} calls={result['network_calls']}",flush=True)
            finally:
                # Don't generate unrelated memories during evaluation shutdown.
                brain.store.close()
    replies=[t['reply'] for c in result['cases'] for t in c['turns']]
    result['metrics']={'replies':len(replies),'actions':sum('（' in x for x in replies),
                       'ellipsis':sum('…' in x for x in replies),'questions':sum('？' in x or '?' in x for x in replies)}
    result['finished']=datetime.now().isoformat()
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result['metrics'],ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
