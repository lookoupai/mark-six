import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from strategy_test_support import NUMBER_SPEC, StrategyTestCase


class ChatManagementTests(StrategyTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.user('owner')
        self.author = self.user('author')
        self.space = self.workspace(self.owner)
        self.member(self.space, self.author)
        self.login(self.author)
        self.base = f'/api/v2/workspaces/{self.space.id}/strategy-assistant/sessions'
        self.strategy = self.request('POST', f'/api/v2/workspaces/{self.space.id}/builtin-strategies/zodiac_rebound/clone',
                                     json={'name':'我的生肖遗漏回补'}).json['item']

    def session(self, strategy=None):
        return self.request('POST', self.base, json={'strategy_id':strategy}).json['session_id']

    def reply(self, session_id, **fields):
        response = Mock()
        response.json.return_value = {'choices':[{'message':{'content':json.dumps({'reply':'可以沿当前生肖规则讨论40期热门。','proposal':None})}}]}
        with patch.object(self.module, 'get_ai_config', return_value={'api_key':'test','api_url':'https://example.invalid','model':'test'}), \
             patch('strategy_platform.api.requests.post', return_value=response) as provider:
            result = self.request('POST', f'{self.base}/{session_id}/messages', json={
                'message':'我想改成40期的热门筛选，算是和现在遗漏回补相反的预测方向，可以吗', **fields})
        return result, provider

    def test_wrong_or_lost_edit_target_cannot_call_provider(self):
        session_id = self.session()
        result, provider = self.reply(session_id, expected_strategy_id=self.strategy['id'])
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json['error'], 'chat_strategy_mismatch')
        provider.assert_not_called()
        bad = self.request('POST', self.base, json={'expected_strategy_id':self.strategy['id']})
        self.assertEqual(bad.status_code, 409)

    def test_bound_prompt_retains_named_zodiac_output_and_explains_hot_direction(self):
        session_id = self.session(self.strategy['id'])
        result, provider = self.reply(session_id, expected_strategy_id=self.strategy['id'])
        self.assertEqual(result.status_code, 200)
        prompt = provider.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('当前正在修改已有策略', prompt)
        self.assertIn('我的生肖遗漏回补', prompt)
        self.assertIn(self.strategy['id'], prompt)
        self.assertIn('综合六肖', prompt)
        self.assertIn('精选四肖', prompt)
        self.assertIn('不要擅自切换成号码预测', prompt)
        self.assertIn('zodiac_groups', prompt)

    def test_unbound_history_can_be_explicitly_attached_once(self):
        from models import StrategyChatMessage, StrategyProposal, StrategyChatSession, db
        session_id = self.session()
        db.session.add(StrategyChatMessage(session_id=session_id, role='user', content='原来的40期讨论'))
        proposal = StrategyProposal(session_id=session_id, created_by_user_id=self.author.id, status='validated',
            proposed_spec_json=json.dumps({'name':'旧草案','spec':NUMBER_SPEC}), diff_json='{}')
        db.session.add(proposal)
        db.session.commit()
        url = f'{self.base}/{session_id}/strategy'
        self.assertEqual(self.request('PATCH', url, json={'strategy_id':self.strategy['id']}).status_code, 400)
        attached = self.request('PATCH', url, json={'strategy_id':self.strategy['id'], 'confirm':True})
        self.assertEqual(attached.status_code, 200, attached.json)
        db.session.expire_all()
        self.assertEqual(proposal.status, 'superseded')
        self.assertIsNotNone(db.session.get(StrategyChatSession, session_id).strategy_id)
        self.assertEqual(StrategyChatMessage.query.filter_by(session_id=session_id, role='user').one().content, '原来的40期讨论')
        second = self.request('POST', f'/api/v2/workspaces/{self.space.id}/strategies', json={'name':'另一个','spec':NUMBER_SPEC}).json['item']
        self.assertEqual(self.request('PATCH', url, json={'strategy_id':second['id'], 'confirm':True}).status_code, 409)
        self.login(self.owner)
        self.assertEqual(self.request('PATCH', url, json={'strategy_id':self.strategy['id'],'confirm':True}).status_code, 403)

    def test_delete_own_chat_keeps_published_strategy_and_cleans_private_history(self):
        from models import (StrategyChatMessage, StrategyChatSession, StrategyProposal, StrategyDefinition,
                            StrategyRevision, StrategyRun, StrategyBackupOutbox, BacktestRun, db)
        from strategy_platform.service import add_custom_strategy_run
        session_id = self.session(self.strategy['id'])
        definition = StrategyDefinition.query.filter_by(public_id=self.strategy['id']).one()
        revision = db.session.get(StrategyRevision, definition.active_revision_id)
        backtest = BacktestRun(name='private chat test', strategies='rule_dsl', payload='{}')
        db.session.add(backtest)
        db.session.flush()
        db.session.add(StrategyProposal(session_id=session_id, strategy_id=definition.id, base_revision_id=revision.id,
            status='published', backtest_run_id=backtest.id, proposed_spec_json='{}', diff_json='{}', created_by_user_id=self.author.id))
        db.session.add(StrategyChatMessage(session_id=session_id, role='user',content='private text'))
        run = add_custom_strategy_run(self.space.id,definition,revision,self.author.id,'hk','2026001','2026000',{'kind':'number_selection'})
        db.session.commit()
        ids = definition.id, revision.id, run.id, backtest.id
        self.login(self.owner)
        self.assertEqual(self.request('DELETE', f'{self.base}/{session_id}', json={'confirm':True}).status_code, 403)
        self.login(self.author)
        self.assertEqual(self.request('DELETE', f'{self.base}/{session_id}', json={}).status_code, 400)
        if db.engine.dialect.name == 'sqlite':
            from sqlalchemy import text
            db.session.execute(text('PRAGMA foreign_keys=ON'))
        deleted = self.request('DELETE', f'{self.base}/{session_id}', json={'confirm':True})
        self.assertEqual(deleted.status_code,200,deleted.json)
        db.session.expire_all()
        self.assertIsNone(db.session.get(StrategyChatSession,session_id))
        self.assertEqual(StrategyChatMessage.query.filter_by(session_id=session_id).count(),0)
        self.assertEqual(StrategyProposal.query.filter_by(session_id=session_id).count(),0)
        self.assertIsNone(db.session.get(BacktestRun,ids[3]))
        self.assertIsNotNone(db.session.get(StrategyDefinition,ids[0]))
        self.assertIsNotNone(db.session.get(StrategyRevision,ids[1]))
        self.assertIsNotNone(db.session.get(StrategyRun,ids[2]))
        self.assertEqual(self.request('GET', f'{self.base}/{session_id}').status_code,404)

    def test_inflight_reply_cannot_resurrect_deleted_conversation(self):
        from models import StrategyChatSession, StrategyChatMessage, StrategyProposal, db
        session_id = self.session()
        response = Mock()
        response.json.return_value = {'choices':[{'message':{'content':json.dumps({'reply':'too late','proposal':{'name':'late','spec':NUMBER_SPEC}})}}]}
        def deleted_during_network(*args, **kwargs):
            # Separate connection represents another request during provider IO.
            with db.engine.begin() as connection:
                connection.execute(StrategyChatMessage.__table__.delete().where(StrategyChatMessage.session_id==session_id))
                connection.execute(StrategyChatSession.__table__.delete().where(StrategyChatSession.id==session_id))
            return response
        with patch.object(self.module,'get_ai_config',return_value={'api_key':'test','api_url':'https://example.invalid','model':'test'}), \
             patch('strategy_platform.api.requests.post',side_effect=deleted_during_network):
            result=self.request('POST', f'{self.base}/{session_id}/messages',json={'message':'late'})
        self.assertEqual(result.status_code,409,result.json)
        self.assertEqual(result.json['error'],'chat_changed')
        db.session.expire_all()
        self.assertEqual(StrategyChatMessage.query.filter_by(session_id=session_id).count(),0)
        self.assertEqual(StrategyProposal.query.filter_by(session_id=session_id).count(),0)

    def test_deleted_chat_during_backtest_does_not_leave_orphan_results(self):
        from models import StrategyChatSession, StrategyChatMessage, StrategyProposal, BacktestRun, db
        from test_zodiac_rebound import example_history, MAPPING
        import strategy_platform.api as api
        session_id=self.session()
        proposal=StrategyProposal(session_id=session_id,created_by_user_id=self.author.id,status='validated',
            proposed_spec_json=json.dumps({'name':'private','spec':NUMBER_SPEC}),diff_json='{}')
        db.session.add(proposal);db.session.commit()
        proposal_id=proposal.id
        original=api.execute_workspace_spec
        deleted=False
        def run_then_delete(*args,**kwargs):
            nonlocal deleted
            result=original(*args,**kwargs)
            if not deleted:
                with db.engine.begin() as connection:
                    connection.execute(StrategyProposal.__table__.delete().where(StrategyProposal.session_id==session_id))
                    connection.execute(StrategyChatMessage.__table__.delete().where(StrategyChatMessage.session_id==session_id))
                    connection.execute(StrategyChatSession.__table__.delete().where(StrategyChatSession.id==session_id))
                deleted=True
            return result
        with patch.object(self.module,'_load_backtest_draws_from_db',return_value=example_history()), \
             patch.object(self.module,'_get_number_to_zodiac_map',return_value=MAPPING), \
             patch.object(api,'execute_workspace_spec',side_effect=run_then_delete):
            result=self.request('POST',f'/api/v2/workspaces/{self.space.id}/strategy-proposals/{proposal_id}/backtests',json={'region':'macau'})
        self.assertEqual(result.status_code,409,result.json)
        self.assertEqual(BacktestRun.query.count(),0)

    def test_inflight_reply_rejects_reused_session_and_message_ids(self):
        from datetime import timedelta
        from models import StrategyChatSession, StrategyChatMessage, StrategyProposal, db
        response = Mock()
        response.json.return_value = {'choices':[{'message':{'content':json.dumps({
            'reply':'old reply', 'proposal':{'name':'old proposal', 'spec':NUMBER_SPEC}})}}]}
        for changed_field in ('session_created_at', 'message_created_at', 'message_content'):
            with self.subTest(changed_field=changed_field):
                session_id = self.session()
                replacement = {}
                def recreate_during_network(*args, **kwargs):
                    # SQLite may reuse both integer primary keys after deletion.
                    with db.engine.begin() as connection:
                        sessions = StrategyChatSession.__table__
                        messages = StrategyChatMessage.__table__
                        chat = dict(connection.execute(sessions.select().where(sessions.c.id == session_id)).mappings().one())
                        message = dict(connection.execute(messages.select().where(messages.c.session_id == session_id)).mappings().one())
                        connection.execute(messages.delete().where(messages.c.session_id == session_id))
                        connection.execute(sessions.delete().where(sessions.c.id == session_id))
                        if changed_field == 'session_created_at':
                            chat['created_at'] += timedelta(seconds=1)
                        elif changed_field == 'message_created_at':
                            message['created_at'] += timedelta(seconds=1)
                        else:
                            message['content'] = 'new request'
                        connection.execute(sessions.insert().values(**chat))
                        connection.execute(messages.insert().values(**message))
                        replacement.update(message)
                    return response
                with patch.object(self.module,'get_ai_config',return_value={'api_key':'test','api_url':'https://example.invalid','model':'test'}), \
                     patch('strategy_platform.api.requests.post',side_effect=recreate_during_network):
                    result = self.request('POST', f'{self.base}/{session_id}/messages',json={'message':'old request'})
                self.assertEqual(result.status_code,409,result.json)
                self.assertEqual(result.json['error'],'chat_changed')
                db.session.expire_all()
                surviving = StrategyChatMessage.query.filter_by(session_id=session_id).all()
                self.assertEqual(len(surviving),1)
                self.assertEqual(surviving[0].content,replacement['content'])
                self.assertEqual(StrategyProposal.query.filter_by(session_id=session_id).count(),0)

    def test_session_search_stays_within_current_user(self):
        from models import StrategyChatSession,db
        for title in ('40期研究','其他讨论'):
            db.session.add(StrategyChatSession(workspace_id=self.space.id,user_id=self.author.id,title=title))
        db.session.add(StrategyChatSession(workspace_id=self.space.id,user_id=self.owner.id,title='40期秘密'))
        db.session.commit()
        response=self.request('GET',self.base+'?q=40期')
        self.assertEqual([item['title'] for item in response.json['items']],['40期研究'])


class BackupScheduleTests(StrategyTestCase):
    def test_hourly_scheduler_and_idle_logging(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.events import JobExecutionEvent, EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
        from datetime import datetime
        module=self.module
        original=module._scheduler
        scheduler=None
        try:
            module._scheduler=None
            with patch.dict('os.environ',{'ENABLE_SCHEDULER':'1'}), \
                 patch.object(module,'_try_acquire_scheduler_lock',return_value=True), \
                 patch('atexit.register'), patch.object(BackgroundScheduler,'start'):
                scheduler=module.start_scheduler()
            job=scheduler.get_job('strategy_github_backup_outbox')
            self.assertEqual(job.trigger.interval.total_seconds(),3600)
            listener=next(callback for callback,mask in scheduler._listeners if mask & EVENT_JOB_EXECUTED)
            with patch('builtins.print') as output:
                listener(JobExecutionEvent(EVENT_JOB_EXECUTED,job.id,'default',datetime.now(),retval=0))
                output.assert_not_called()
                listener(JobExecutionEvent(EVENT_JOB_EXECUTED,job.id,'default',datetime.now(),retval=2))
                self.assertTrue(output.called)
            self.assertTrue(any(mask & EVENT_JOB_ERROR for _,mask in scheduler._listeners))
        finally:
            module._scheduler=original

    def test_backup_job_returns_count_and_rolls_back_inside_context(self):
        with patch('strategy_platform.backup.process_strategy_backup_outbox',return_value=0):
            self.assertEqual(self.module.process_strategy_backup_outbox_job(),0)
        with patch('strategy_platform.backup.process_strategy_backup_outbox',side_effect=RuntimeError('sensitive detail')):
            with self.assertRaisesRegex(RuntimeError,'strategy_backup_job_failed'):
                self.module.process_strategy_backup_outbox_job()
