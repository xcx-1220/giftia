#!/usr/bin/env python3
"""Conversation continuity, evidenced state, maintenance isolation and request recovery."""
from __future__ import annotations

import dataclasses
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from giftia.brain import Giftia
from giftia.config import load_settings
from giftia.conversation import Conversation, reply_focus
from giftia.llm import MockLLM, MemoryDraft
from giftia.maintenance import Maintenance
from giftia.memory import consolidate
from giftia.mood import tone_of
from giftia.requests import RequestLedger
from giftia.store import Store
from giftia.webgate import WebGate
from tools.quality_repair import repair
from giftia.push import LastMessage, notify_all
from deploy.quality_release import repair_completion_mood, safe_path, files as release_files


class BlockingLLM(MockLLM):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.worker = None

    def extract_memories(self, transcript, **kwargs):
        self.worker = threading.get_ident()
        self.entered.set()
        self.release.wait(5)
        return [MemoryDraft(summary='搭档喜欢读小说', source_ids=[1], kind='fact')]


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dataclasses.replace(load_settings(force_mock=True), root=Path(self.tmp.name),
                                      dream=dataclasses.replace(load_settings(force_mock=True).dream, enabled=False))
        self.brain = Giftia(self.cfg)
        self.brain.set_handler_id('owner', local=True)

    def tearDown(self):
        if not getattr(self.brain, '_test_closed', False):
            if hasattr(self.brain, '_maintenance'):
                self.brain._maintenance.close()
            self.brain.store.close()
        self.tmp.cleanup()

    def msg(self, text, *, scope='private', subject='', role='user', session=None):
        return self.brain.store.add_message(session or self.brain.session_id, self.brain.instance_no,
                                            role, text, self.brain.clock.iso(), scope=scope, subject_id=subject)

    def test_completion_is_not_negative(self):
        for text in ['吃完了', '我做完了', '看完了', '处理完了', '艾拉，吃完了哦，我得开始干活了']:
            self.assertEqual(tone_of(text), 0, text)
        self.assertLess(tone_of('完了，我失败了'), -0.4)
        self.assertLess(tone_of('我喜欢的游戏停服了'), -0.4)
        self.assertLess(tone_of('我升职失败了'), -0.4)
        self.assertLess(tone_of('升职了，但我一点也高兴不起来'), -0.4)

    def test_turn_focus_respects_feedback_and_farewell(self):
        self.assertIn('不换个问题', reply_focus('别一直问细节啦，感觉像在查户口', []))
        self.assertIn('不询问行程', reply_focus('我得开始干活了', []))
        self.assertIn('不再采集', reply_focus('就是随便逛逛', [
            {'role':'assistant','content':'什么店？'}, {'role':'assistant','content':'买了什么？'}]))

    def test_no_time_tags_inside_assistant_history(self):
        rows=[{'role':'assistant','content':'我在。','ts':self.brain.clock.iso()}]
        window=self.brain._window(rows)
        self.assertEqual(window,[{'role':'assistant','content':'我在。'}])

    def test_explicit_feedback_is_immediate_and_retry_is_bounded(self):
        class QuestionLLM(MockLLM):
            def __init__(self,cfg):super().__init__(cfg);self.calls=0
            def chat(self,messages,**kwargs):
                self.calls+=1
                if any('上一稿仍在问问题' in m.get('content','') for m in messages if m['role']=='system'):
                    return '好，我先听你说。'
                return '那你今天过得好吗？'
        client=QuestionLLM(self.cfg.model);self.brain.llm=client
        reply=self.brain.handle('别一直问细节，感觉像在查户口',user_id='owner')
        self.assertNotIn('？',reply)
        self.assertEqual(client.calls,2)
        self.assertEqual(self.brain.store.count_messages(),2)
        self.assertIn('连续追问',self.brain.conversation_state().render(self.brain.clock.now()))

    def test_cross_midnight_and_scope(self):
        old_day = (self.brain.clock.now()-timedelta(days=1)).strftime('%Y%m%d')
        self.msg('上一天的未完话题', session=f'i1-m-{old_day}')
        self.msg('朋友的私事', scope='person', subject='friend', session='i1-pfriend-20261024')
        self.assertEqual([x['content'] for x in self.brain.context_history()], ['上一天的未完话题'])
        self.brain.clock.advance(48)
        self.assertEqual(self.brain.context_history(), [])

    def test_full_subject_prevents_truncated_id_collision(self):
        self.msg('不能串给另一个人', scope='person', subject='abcdefghijkl-one', session='i1-pabcdefghijkl-20261025')
        self.assertEqual(self.brain.store.context_messages(1,'i1-pabcdefghijkl-', 'person', 'abcdefghijkl-two'), [])

    def test_state_needs_user_evidence_and_can_finish(self):
        mid = self.msg('不要一直问我')
        state = self.brain.conversation_state()
        state.update(MemoryDraft(summary='减少连续追问',kind='interaction_feedback',behavior_key='questions',source_ids=[mid]),
                     self.brain.store.messages_by_ids([mid]),self.brain.clock.now())
        self.assertIn('减少连续追问',state.render(self.brain.clock.now()))
        other = self.msg('我答得够快了',role='assistant')
        state.update(MemoryDraft(summary='必须每次问够不够快',kind='preference',source_ids=[other]),
                     self.brain.store.messages_by_ids([other]),self.brain.clock.now())
        self.assertNotIn('必须每次',state.render(self.brain.clock.now()))
        state.update(MemoryDraft(summary='减少连续追问',kind='interaction_feedback',behavior_key='questions',source_ids=[mid],status='dismissed'),
                     self.brain.store.messages_by_ids([mid]),self.brain.clock.now())
        self.assertEqual(state.render(self.brain.clock.now()),'')

    def test_state_retraction_and_epoch(self):
        mid=self.msg('我明天考试')
        state=self.brain.conversation_state()
        state.update(MemoryDraft(summary='明天考试',kind='open_loop',source_ids=[mid]),self.brain.store.messages_by_ids([mid]),self.brain.clock.now())
        self.assertTrue(state.render(self.brain.clock.now()))
        self.brain.store.conn.execute('UPDATE messages SET recalled=1 WHERE id=?',(mid,));self.brain.store.conn.commit()
        self.assertEqual(state.render(self.brain.clock.now()),'')
        self.brain.store.meta_set('handler_bound_msg_id',mid)
        self.assertNotEqual(state.key,self.brain.conversation_state().key)

    def test_new_owner_cannot_inherit_state_without_longterm_memories(self):
        mid=self.msg('我不喜欢被反复追问')
        state=self.brain.conversation_state()
        state.update(MemoryDraft(summary='减少追问',kind='interaction_feedback',source_ids=[mid]),
                     self.brain.store.messages_by_ids([mid]),self.brain.clock.now())
        self.brain.set_handler_id('new-owner')
        self.assertNotEqual(state.key,self.brain.conversation_state().key)
        self.assertEqual(self.brain.conversation_state().render(self.brain.clock.now()),'')
        self.assertEqual(self.brain.context_history(),[])

    def test_outreach_floor_is_applied_after_a_visitor_turn(self):
        mid=self.msg('前任的原话')
        self.brain.set_handler_id('new-owner')
        self.brain.scope_key='pvisitor'
        raw=self.brain.store.messages_by_ids([mid])
        self.assertEqual(self.brain._since_floor(raw,private=True),[])

    def test_state_expires_and_respects_degradation(self):
        mid=self.msg('这周三去参加科目二考试')
        state=self.brain.conversation_state()
        state.update(MemoryDraft(summary='这周三去参加科目二考试',kind='topic',source_ids=[mid]),
                     self.brain.store.messages_by_ids([mid]),self.brain.clock.now())
        terminal=next(s for s in self.cfg.stages if s.name=='terminal')
        self.assertNotIn('这周三去参加科目二考试',state.render(self.brain.clock.now(),stage=terminal))
        self.brain.clock.advance(73)
        self.assertEqual(state.render(self.brain.clock.now()),'')

    def test_transactional_evidence_and_no_performance_memory(self):
        mid=self.msg('我喜欢读小说')
        drafts=[MemoryDraft(summary='搭档喜欢读小说',kind='preference',source_ids=[mid]),
                MemoryDraft(summary='她答得很快并问够不够快',kind='event',source_ids=[mid])]
        consolidate(self.brain.store,None,self.cfg,session_id=self.brain.session_id,now_iso=self.brain.clock.iso(),force=True,
                    prepared=(self.brain.store.messages_by_ids([mid]),drafts),conversation=self.brain.conversation_state())
        self.assertEqual(len(self.brain.store.active_memories(1,Store.ALL_SCOPES)),1)
        self.assertEqual(self.brain.store.conn.execute('SELECT count(*) FROM memory_evidence').fetchone()[0],1)

    def test_historical_repair_reversible_and_idempotent(self):
        mid=self.brain.store.add_memory(instance_no=1,created_ts=self.brain.clock.iso(),happened_ts=self.brain.clock.iso(),
                                        summary='道别时艾拉特意答得很快，问这次够不够快')
        db=self.cfg.db_path
        self.assertEqual(len(repair(db)['candidates']),1)
        self.assertEqual(repair(db,apply=True)['changed'],[mid])
        self.assertEqual(repair(db,apply=True)['changed'],[])
        self.assertEqual(self.brain.store.active_memories(1,Store.ALL_SCOPES),[])
        self.assertEqual(self.brain.store.count_memories(1),1)
        self.assertEqual(repair(db,rollback=True)['changed'],[mid])
        self.assertEqual(len(self.brain.store.active_memories(1,Store.ALL_SCOPES)),1)

    def test_release_only_repairs_proved_completion_mood(self):
        mid=self.msg('吃完了，我开始干活了')
        prefix='mood:i1:'
        for key,value in dict(key='worried',source='his_tone',since=self.brain.clock.iso(),
                              until=(self.brain.clock.now()+timedelta(hours=10)).isoformat(),intensity='1.0').items():
            self.brain.store.state_set(prefix+key,value)
        result=repair_completion_mood(self.cfg.db_path)
        self.assertEqual(result['source_message_id'],mid)
        self.assertEqual(self.brain.store.state_get(prefix+'key'),'calm')
        self.assertEqual(result['previous'][prefix+'key'],'worried')
        self.assertIsNone(repair_completion_mood(self.cfg.db_path))

    def test_release_paths_do_not_include_protected_config(self):
        root=Path(__file__).resolve().parent.parent
        picked=release_files(root)
        self.assertFalse(any(p.name=='.env' or p.name.endswith('.local.toml') for p in picked))
        with self.assertRaises(ValueError):safe_path(root,Path('../outside'))

    def test_maintenance_never_blocks_reply_or_uses_sqlite_in_worker(self):
        llm=BlockingLLM(self.cfg.model)
        manager=Maintenance(self.brain,llm)
        self.brain._maintenance=manager
        for _ in range(3): self.brain.handle('今天想看小说',user_id='owner',defer_maintenance=True)
        manager.poll()
        self.assertTrue(llm.entered.wait(2))
        before=time.monotonic()
        reply=self.brain.handle('我还在',user_id='owner',defer_maintenance=True)
        self.assertTrue(reply)
        self.assertLess(time.monotonic()-before,1)
        self.assertNotEqual(llm.worker,threading.get_ident())
        llm.release.set()
        for _ in range(100):
            manager.poll(start=False)
            if not manager.running: break
            time.sleep(.01)
        self.assertFalse(manager.running)
        self.assertTrue(self.brain.store.active_memories(1,Store.ALL_SCOPES))

    def test_maintenance_discards_results_after_retraction(self):
        llm=BlockingLLM(self.cfg.model);manager=Maintenance(self.brain,llm);self.brain._maintenance=manager
        for _ in range(3):self.brain.handle('今天想看小说',user_id='owner',defer_maintenance=True)
        manager.poll();self.assertTrue(llm.entered.wait(2))
        self.brain.store.conn.execute('UPDATE messages SET recalled=1 WHERE id=1');self.brain.store.conn.commit()
        llm.release.set()
        for _ in range(100):
            manager.poll(start=False)
            if not manager.running:break
            time.sleep(.01)
        self.assertEqual(self.brain.store.active_memories(1,Store.ALL_SCOPES),[])
        self.assertEqual(self.brain.store._one('SELECT status FROM maintenance_tasks ORDER BY id LIMIT 1')['status'],'stale')

    def test_restart_recovers_queued_maintenance(self):
        manager=Maintenance(self.brain)
        for _ in range(6):self.msg('有依据的消息')
        manager.schedule();manager.close()
        recovered=Maintenance(self.brain);self.brain._maintenance=recovered
        self.assertGreater(self.brain.store._one("SELECT count(*) n FROM maintenance_tasks WHERE status='queued'")['n'],0)

    def test_request_ledger_recovers_completed_result(self):
        path=Path(self.tmp.name)/'requests.json'; ledger=RequestLedger(path)
        row=dict(text='在吗',role='owner',user_id='',principal='p',request_id='a'*32)
        self.assertTrue(ledger.reserve('p:'+'a'*32,row))
        self.assertFalse(ledger.reserve('p:'+'a'*32,row))
        ledger.update('p:'+'a'*32,status='completed',reply='我在')
        restored=RequestLedger(path)
        self.assertEqual(restored.get('p:'+'a'*32)['reply'],'我在')
        self.assertIsNone(restored.get('another:'+'a'*32))
        with self.assertRaises(ValueError):ledger.reserve('p:'+'a'*32,dict(row,text='不同内容'))

    def test_request_capacity_and_identity(self):
        gate=WebGate(port=0,secret='owner',friend_pass='friends',static_dir=Path('web'),data_dir=Path(self.tmp.name)/'web')
        a=gate.principal('friend','visitor-a');b=gate.principal('friend','visitor-b')
        self.assertNotEqual(a,b)
        first=gate.submit('在吗','friend','visitor-a','a'*32,a)
        self.assertIs(first,gate.submit('在吗','friend','visitor-a','a'*32,a))
        self.assertEqual(gate.pending,1)
        owner=gate.principal('owner');gate._owner_epoch='new-instance'
        self.assertNotEqual(owner,gate.principal('owner'))
        for i in range(4):gate.submit('消息','owner','',str(i)*32,gate.principal('owner'))
        import queue
        with self.assertRaises(queue.Full):gate.submit('超额','owner','','z'*32,gate.principal('owner'))
        no_friends=WebGate(port=0,secret='owner',static_dir=Path('web'),data_dir=Path(self.tmp.name)/'no-friends')
        self.assertEqual(no_friends.role_of_cookie(no_friends.token('friend')),'')

    def test_failed_push_restores_previous_message(self):
        last=LastMessage(Path(self.tmp.name)/'last.json')
        last.set('以前确实发过的消息');previous=last.get()
        class Subs:
            def load(self):return [{'endpoint':'https://fcm.googleapis.com/fake'}]
            def remove(self,endpoint):pass
        with patch('giftia.push.send_push',return_value=(False,500,'failure')):
            self.assertEqual(notify_all(Subs(),last,'没发出去',private_b64='fake',public_b64='fake',subject='test'),(0,1))
        self.assertEqual(last.get(),previous)
        token=last.set('第一条');last.set('另一条更新的消息')
        last.restore_if(token,previous)
        self.assertEqual(last.get()['text'],'另一条更新的消息')


if __name__=='__main__':
    unittest.main(verbosity=2)
