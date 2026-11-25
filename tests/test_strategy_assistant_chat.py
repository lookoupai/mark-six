from copy import deepcopy
import json
from unittest.mock import Mock, patch

from strategy_test_support import NUMBER_SPEC, StrategyTestCase


class StrategyAssistantChatTests(StrategyTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.user('chat-owner')
        self.author = self.user('chat-author')
        self.space = self.workspace(self.owner)
        self.member(self.space, self.author)
        self.login(self.author)
        self.base = f'/api/v2/workspaces/{self.space.id}/strategy-assistant/sessions'

    def create_session(self, strategy_id=None):
        result = self.request('POST', self.base, json={'strategy_id': strategy_id})
        self.assertEqual(result.status_code, 201)
        return result.json['session_id']

    def send(self, session_id, content, message='我想做短期版的，统计改成 10 期', **fields):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': content}}]}
        with patch.object(self.module, 'get_ai_config', return_value={'api_key': 'test-only', 'api_url': 'https://example.invalid', 'model': 'test'}), \
             patch('strategy_platform.api.requests.post', return_value=response) as provider:
            result = self.request('POST', f'{self.base}/{session_id}/messages', json={'message': message, **fields})
        return result, provider.call_args

    def test_provider_client_restriction_is_an_error_not_an_assistant_reply(self):
        from models import StrategyChatMessage, StrategyProposal
        session_id = self.create_session()
        restriction = '本站仅限主流 Coding 客户端/CLI 使用，支持 Codex。当前客户端不支持。'
        result, _ = self.send(session_id, restriction)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json['error'], 'ai_client_restricted')
        self.assertIn('网页后端', result.json['message'])
        self.assertEqual(StrategyChatMessage.query.filter_by(session_id=session_id, role='assistant').count(), 0)
        self.assertEqual(StrategyProposal.query.count(), 0)
        restored = self.request('GET', f'{self.base}/{session_id}').json
        self.assertEqual(restored['messages'][-1]['role'], 'service')
        self.assertIsNotNone(restored['retry_message_id'])

    def test_empty_or_malformed_provider_payload_does_not_report_success(self):
        session_id = self.create_session()
        for content in ('', '   ', '{"unexpected":"value"}', '{"reply":null,"proposal":null}'):
            with self.subTest(content=content):
                result, _ = self.send(session_id, content)
                self.assertEqual(result.status_code, 502)
                self.assertEqual(result.json['error'], 'ai_response_invalid')

    def test_retry_reuses_failed_user_turn_and_returns_visible_reply(self):
        from models import StrategyChatMessage
        session_id = self.create_session()
        failed, _ = self.send(session_id, '')
        message_id = failed.json['user_message_id']
        successful, _ = self.send(session_id, json.dumps({'reply': '可以先把统计窗口改成 10 期，再讨论重复生肖。', 'proposal': None}), retry_message_id=message_id)
        self.assertEqual(successful.status_code, 200)
        self.assertIn('10 期', successful.json['reply'])
        self.assertEqual(StrategyChatMessage.query.filter_by(session_id=session_id, role='user').count(), 1)
        self.assertEqual(StrategyChatMessage.query.filter_by(session_id=session_id, role='assistant').count(), 1)

    def test_conversation_context_includes_latest_unpublished_rule(self):
        from models import StrategyDefinition
        session_id = self.create_session()
        spec = {**NUMBER_SPEC, 'window': 10}
        first, _ = self.send(session_id, json.dumps({'reply': '已整理 10 期版，尚未发布。', 'proposal': {'name': '短期规则', 'change_summary': '20 期改为 10 期', 'spec': spec}}))
        self.assertEqual(first.status_code, 200)
        second, call = self.send(session_id, json.dumps({'reply': '保留 10 期。你是想排除近期出现的生肖，还是增加其重复出现时的覆盖？', 'proposal': None}), message='保留10期，先讨论防重复，不要发布')
        self.assertEqual(second.status_code, 200)
        prompt = call.kwargs['json']['messages'][0]['content']
        self.assertIn('待确认草案', prompt)
        self.assertIn('"window": 10', prompt)
        self.assertIn('不要把尚未支持的规则描述为已实现', prompt)
        self.assertEqual(StrategyDefinition.query.filter_by(workspace_id=self.space.id).count(), 0)
        restored = self.request('GET', f'{self.base}/{session_id}').json
        self.assertEqual(len(restored['messages']), 4)
        self.assertEqual(restored['proposals'][0]['proposal']['spec']['window'], 10)
        self.assertEqual(restored['proposals'][0]['status'], 'validated')
        self.assertIsNone(restored['retry_message_id'])

    def test_invalid_proposal_feedback_survives_refresh_alongside_reply(self):
        from models import StrategyProposal
        session_id = self.create_session()
        result, _ = self.send(session_id, json.dumps({'reply':'我尝试整理了你的想法，需要检查可执行范围。',
            'proposal':{'name':'不支持的草案','spec':{**NUMBER_SPEC,'python':'unsafe'}}}))
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json['validation']['valid'])
        restored = self.request('GET', f'{self.base}/{session_id}').json
        self.assertTrue(any(row['role']=='assistant' and '尝试整理' in row['content'] for row in restored['messages']))
        self.assertTrue(any(row['role']=='service' and '尚未保存或发布' in row['content'] for row in restored['messages']))
        self.assertEqual(StrategyProposal.query.count(), 0)

    def test_sessions_are_private_paginated_and_reopen_same_strategy(self):
        from models import StrategyChatSession
        strategy = self.request('POST', f'/api/v2/workspaces/{self.space.id}/strategies', json={'name': '会话策略', 'spec': NUMBER_SPEC}).json['item']
        first = self.create_session(strategy['id'])
        second = self.create_session()
        listed = self.request('GET', self.base+'?limit=1').json
        self.assertEqual(len(listed['items']), 1)
        self.assertTrue(listed['has_more'])
        matching = self.request('GET', self.base+f'?strategy_id={strategy["id"]}').json
        self.assertEqual([row['id'] for row in matching['items']], [first])
        self.assertEqual(matching['items'][0]['strategy_name'], '会话策略')
        restored = self.request('GET', f'{self.base}/{first}').json
        self.assertEqual(restored['strategy']['id'], strategy['id'])
        self.assertEqual(restored['strategy']['revision_id'], strategy['active_revision_id'])
        self.assertEqual(StrategyChatSession.query.count(), 2)
        self.login(self.owner)
        self.assertEqual(self.request('GET', self.base).json['items'], [])
        self.assertEqual(self.request('GET', f'{self.base}/{first}').status_code, 403)

    def test_publishing_new_strategy_binds_conversation_and_discards_stale_draft_context(self):
        from models import StrategyChatSession, StrategyDefinition, StrategyProposal, LotteryDraw, db
        from test_zodiac_rebound import example_history, MAPPING
        session_id = self.create_session()
        for window in (20, 10):
            proposal, _ = self.send(session_id, json.dumps({'reply': f'窗口改为{window}期', 'proposal': {
                'name': '连续讨论策略', 'change_summary': f'窗口{window}期', 'spec': {**NUMBER_SPEC, 'window': window}}}))
        latest_id = proposal.json['proposal_id']
        for draw in example_history():
            db.session.add(LotteryDraw(region='macau', draw_id=draw['id'], draw_date=draw['date'],
                normal_numbers=','.join(draw['no']), special_number=draw['sno'], special_zodiac=draw['sno_zodiac'], raw_zodiac=draw['raw_zodiac']))
        db.session.commit()
        with patch.object(self.module, '_get_number_to_zodiac_map', return_value=MAPPING):
            backtest = self.request('POST', f'/api/v2/workspaces/{self.space.id}/strategy-proposals/{latest_id}/backtests', json={'region':'macau'})
        self.assertEqual(backtest.status_code, 200)
        published = self.request('POST', f'/api/v2/workspaces/{self.space.id}/strategy-proposals/{latest_id}/publish', json={'confirm':True})
        self.assertEqual(published.status_code, 200)
        definition = StrategyDefinition.query.filter_by(public_id=published.json['item']['id']).one()
        self.assertEqual(db.session.get(StrategyChatSession, session_id).strategy_id, definition.id)
        followup, call = self.send(session_id, json.dumps({'reply':'继续讨论现有10期版。', 'proposal':None}), message='继续讨论，不要另建策略')
        self.assertEqual(followup.status_code, 200)
        prompt = call.kwargs['json']['messages'][0]['content']
        self.assertIn('当前策略名：连续讨论策略', prompt)
        self.assertNotIn('最近待确认草案', prompt)
        self.assertEqual(StrategyDefinition.query.filter_by(workspace_id=self.space.id).count(), 1)
        self.assertEqual(StrategyProposal.query.filter_by(session_id=session_id, status='validated').count(), 0)
        self.assertEqual(self.request('GET', f'{self.base}/{session_id}').json['strategy']['id'], definition.public_id)

    def test_zodiac_clone_can_run_through_public_api(self):
        from test_zodiac_rebound import example_history, MAPPING
        item = self.request('POST', f'/api/v2/workspaces/{self.space.id}/builtin-strategies/zodiac_rebound/clone', json={'name':'短期生肖'}).json['item']
        with patch.object(self.module, '_get_prediction_data', return_value=(example_history(), 2026)), \
             patch.object(self.module, '_get_next_period', return_value='2026326'), \
             patch.object(self.module, '_get_number_to_zodiac_map', return_value=MAPPING):
            result = self.request('POST', f'/api/v2/workspaces/{self.space.id}/strategies/{item["id"]}/runs', json={'region':'macau'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual([len(group['items']) for group in result.json['result']['groups']], [6,4])

    def test_zodiac_proposal_backtest_uses_target_and_history_mappings(self):
        from models import LotteryDraw, db
        from test_zodiac_rebound import example_history, MAPPING
        session_id = self.create_session()
        spec = {'schema_version':1,'kind':'builtin_config','strategy_key':'zodiac_rebound',
                'parameters':{'window':10,'parity_streak_threshold':5},'regions':['macau']}
        proposal, _ = self.send(session_id, json.dumps({'reply':'先回测10期草案。','proposal':{'name':'短期生肖','spec':spec}}))
        for draw in example_history():
            db.session.add(LotteryDraw(region='macau',draw_id=draw['id'],draw_date=draw['date'],
                normal_numbers=','.join(draw['no']),special_number=draw['sno'],special_zodiac=draw['sno_zodiac'],raw_zodiac=draw['raw_zodiac']))
        db.session.commit()
        with patch.object(self.module, '_get_number_to_zodiac_map', return_value=MAPPING):
            response = self.request('POST', f'/api/v2/workspaces/{self.space.id}/strategy-proposals/{proposal.json["proposal_id"]}/backtests', json={'region':'macau'})
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json['backtest']['summary']['groups']['six']['total'], 0)
        self.assertGreater(response.json['backtest']['summary']['groups']['four']['total'], 0)

    def test_chat_page_and_message_pagination_do_not_drop_history(self):
        from models import StrategyChatMessage, db
        session_id = self.create_session()
        for index in range(55):
            db.session.add(StrategyChatMessage(session_id=session_id, role='user' if index%2 == 0 else 'assistant', content=f'历史消息 {index}'))
        db.session.commit()
        latest = self.request('GET', f'{self.base}/{session_id}?limit=20').json
        self.assertEqual(len(latest['messages']), 20)
        self.assertTrue(latest['has_more_messages'])
        self.assertEqual(latest['messages'][-1]['content'], '历史消息 54')
        older = self.request('GET', f'{self.base}/{session_id}?limit=20&before_message_id={latest["messages"][0]["id"]}').json
        self.assertEqual(older['messages'][-1]['content'], '历史消息 34')
        page = self.request('GET', f'/user/strategy-assistant?workspace_id={self.space.id}&session_id={session_id}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('对话记录', page.get_data(as_text=True))
