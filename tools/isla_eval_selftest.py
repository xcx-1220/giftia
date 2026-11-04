"""Protocol, isolation, and experiment-integrity checks; no live API calls."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from giftia.config import load_settings, ModelConfig
from giftia.llm import DeepSeekLLM, MockLLM, LLMError
from tools.isla_character_eval import CallRecorder, blind_compare, classify, configure, fixture, observe


def response(content='好呀', finish='stop'):
    return {'model': 'fixture', 'system_fingerprint': 'fixture-v1',
            'choices': [{'finish_reason': finish, 'message': {'role': 'assistant',
                         'content': content, 'reasoning_content': 'private-reasoning-fixture'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5,
                      'completion_tokens_details': {'reasoning_tokens': 3}}}


class EvalTests(unittest.TestCase):
    def client(self, mode=False):
        return DeepSeekLLM(ModelConfig(thinking=mode), api_key='offline-fixture', retries=3)

    def test_cap_counts_transport_attempts_and_cannot_retry(self):
        client = self.client()
        client._post = lambda payload: response()
        recorder = CallRecorder(1)
        recorder.attach(client)
        self.assertEqual(client.retries, 1)
        client.chat([{'role': 'user', 'content': '你好'}])
        with self.assertRaises(LLMError):
            client.chat([{'role': 'user', 'content': '再来'}])
        self.assertEqual(len(recorder.calls), 1)
        self.assertTrue(recorder.exhausted)

    def test_failed_transport_uses_budget(self):
        client = self.client()
        def fail(payload):
            raise LLMError('fixture failure')
        client._post = fail
        recorder = CallRecorder(1)
        recorder.attach(client)
        with self.assertRaises(LLMError):
            client.chat([])
        self.assertEqual(recorder.calls[0]['status'], 'error')
        self.assertEqual(classify(recorder.calls, '兜底回复'), 'api_error')

    def test_metadata_excludes_reasoning_and_payload(self):
        client = self.client()
        client._post = lambda payload: response()
        recorder = CallRecorder(1)
        recorder.attach(client)
        self.assertEqual(client.chat([{'role': 'user', 'content': 'fixture-user-text'}]), '好呀')
        saved = json.dumps(recorder.calls)
        self.assertNotIn('private-reasoning-fixture', saved)
        self.assertNotIn('fixture-user-text', saved)
        self.assertNotIn('offline-fixture', saved)
        self.assertEqual(recorder.calls[0]['usage']['completion_tokens_details']['reasoning_tokens'], 3)

    def test_thinking_and_non_thinking_payloads(self):
        for enabled in (False, True):
            client = self.client(enabled)
            seen = []
            def post(payload):
                seen.append(payload)
                return response()
            client._post = post
            client.chat([])
            self.assertEqual(seen[0]['thinking']['type'], 'enabled' if enabled else 'disabled')
            self.assertEqual('temperature' in seen[0], not enabled)

    def test_partial_truncation_is_not_success(self):
        self.assertEqual(classify([{'status':'ok','finish_reason':'length','content_chars':3}], '半句话'), 'truncated')
        self.assertEqual(classify([{'status':'ok','finish_reason':'stop','content_chars':0}], '兜底'), 'empty')

    def test_tool_raw_message_preserves_reasoning_in_same_loop(self):
        client = self.client(True)
        data = response(None, 'tool_calls')
        data['choices'][0]['message']['tool_calls'] = [
            {'id':'tool-1', 'type':'function','function':{'name':'recall','arguments':'{}'}}]
        seen = []
        def post(payload):
            seen.append(payload)
            return data if len(seen)==1 else response()
        client._post = post
        reply = client.chat_with_tools([], tools=[{'type':'function','function':{'name':'recall'}}])
        history = [reply.raw_message, {'role':'tool','tool_call_id':'tool-1','content':'fixture'}]
        client.chat_with_tools(history, tools=[{'type':'function','function':{'name':'recall'}}])
        self.assertEqual(seen[1]['messages'][0]['reasoning_content'], 'private-reasoning-fixture')

    def test_experiment_settings_do_not_change_production_settings(self):
        original = load_settings(force_mock=True)
        cfg = configure(original, thinking='low', max_tokens=4096)
        self.assertFalse(original.model.thinking)
        self.assertTrue(cfg.model.thinking)
        self.assertFalse(cfg.memory.tools_enabled)
        self.assertFalse(cfg.memory.background_maintenance)
        self.assertFalse(cfg.dream.enabled)
        self.assertFalse(cfg.outreach.enabled)
        self.assertEqual(cfg.model.max_tokens, 4096)

    def test_fixture_uses_real_stage_selection_without_changing_stage_during_chat(self):
        cfg = configure(load_settings(force_mock=True))
        with tempfile.TemporaryDirectory() as tmp:
            for stage in ['stranger','partner','familiar','close','irreplaceable']:
                case = {'id':stage,'stage':stage,'known_name':stage!='stranger'}
                brain = fixture(cfg, Path(tmp)/stage, case, MockLLM(cfg.model))
                try:
                    before = brain.current_bond().affinity
                    brain.handle('你好呀', user_id='eval-owner')
                    self.assertEqual(brain.current_bond().stage.key, stage)
                    self.assertEqual(brain.current_bond().affinity, before)
                    self.assertTrue(brain.settings.db_path.is_relative_to(Path(tmp)))
                finally:
                    brain.store.close()

    def test_unsafe_fixture_id_rejected_before_database_creation(self):
        cfg = configure(load_settings(force_mock=True))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                fixture(cfg, tmp, {'id':'../escape'}, MockLLM(cfg.model))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_observation_reads_only_fixture_private_memories(self):
        cfg = configure(load_settings(force_mock=True))
        with tempfile.TemporaryDirectory() as tmp:
            brain = fixture(cfg, tmp, {'id':'observe','memories':['搭档喜欢清茶']}, MockLLM(cfg.model))
            try:
                brain.store.add_memory(instance_no=brain.instance_no,created_ts=brain.clock.iso(),happened_ts=brain.clock.iso(),
                                       summary='访客的秘密',scope='person',subject_id='visitor')
                snapshot = observe(brain)
                self.assertIn('搭档喜欢清茶', snapshot['memory_summaries'])
                self.assertNotIn('访客的秘密', snapshot['memory_summaries'])
            finally:
                brain.store.close()

    def test_blind_comparison_rejects_different_inputs(self):
        base = {'id':'daily','repeat':1,'fixture_hash':'one'}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                blind_compare({'cases':[base]}, {'cases':[dict(base,fixture_hash='two')]}, Path(tmp)/'blind.json')


if __name__ == '__main__':
    unittest.main()
