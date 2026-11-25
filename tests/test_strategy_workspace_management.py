from copy import deepcopy
import json
import tempfile
from sqlalchemy import create_engine, text
from strategy_test_support import StrategyTestCase, NUMBER_SPEC
from test_zodiac_rebound import MAPPING, example_history


class WorkspaceManagementTests(StrategyTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.user('workspace-admin', admin=True)
        self.owner = self.user('workspace-owner')
        self.member_user = self.user('workspace-member')
        self.space = self.workspace(self.owner)
        self.member(self.space, self.member_user, 'member')

    def test_owner_and_admin_can_rename_without_changing_acl_or_slug(self):
        from models import StrategyWorkspace, StrategyWorkspaceMembership, StrategyAuditEvent, db
        base = f'/api/v2/workspaces/{self.space.id}'
        self.login(self.member_user)
        self.assertEqual(self.request('PATCH', base, json={'name': 'not allowed'}).status_code, 403)
        self.login(self.owner)
        renamed = self.request('PATCH', base, json={'name': '正式策略工作区', 'expected_name': self.space.name})
        self.assertEqual(renamed.status_code, 200, renamed.json)
        self.login(self.admin)
        renamed = self.request('PATCH', f'/admin/strategy-workspaces/{self.space.id}',
                               json={'name': '研究工作区', 'expected_name': '正式策略工作区'})
        self.assertEqual(renamed.status_code, 200, renamed.json)
        db.session.expire_all()
        self.assertEqual(db.session.get(StrategyWorkspace, self.space.id).name, '研究工作区')
        self.assertEqual(self.space.slug, 'test-workspace')
        self.assertEqual(StrategyWorkspaceMembership.query.filter_by(workspace_id=self.space.id, role='owner').one().user_id, self.owner.id)
        self.assertEqual(StrategyAuditEvent.query.filter_by(action='workspace.renamed').count(), 2)
        stale = self.request('PATCH', f'/admin/strategy-workspaces/{self.space.id}', json={'name': 'stale', 'expected_name': '正式策略工作区'})
        self.assertEqual(stale.status_code, 409)
        for name in ('', 'x' * 121, ['invalid']):
            self.assertEqual(self.request('PATCH', f'/admin/strategy-workspaces/{self.space.id}', json={'name': name}).status_code, 400)

    def test_admin_backup_entry_does_not_grant_workspace_owner_access(self):
        self.login(self.admin)
        page = self.request('GET', '/admin/strategy-workspaces')
        self.assertEqual(page.status_code, 200)
        self.assertIn('GitHub 备份', page.get_data(as_text=True))
        self.assertIn('工作区所有者', page.get_data(as_text=True))
        self.assertEqual(self.request('GET', f'/api/v2/workspaces/{self.space.id}/management/backup/github').status_code, 403)
        own = self.workspace(self.admin, 'admin-owned')
        page = self.request('GET', '/admin/strategy-workspaces').get_data(as_text=True)
        self.assertIn(f'workspace_id={own.id}#strategyBackupSection', page)
        self.assertNotIn(f'workspace_id={self.space.id}#strategyBackupSection', page)

    def test_strategy_name_and_parameters_create_immutable_revision(self):
        from models import StrategyDefinition, StrategyRevision, db
        self.login(self.owner)
        base = f'/api/v2/workspaces/{self.space.id}'
        created = self.request('POST', base+'/builtin-strategies/zodiac_rebound/clone', json={'name': '生肖研究一号'})
        self.assertEqual(created.status_code, 201)
        item = created.json['item']
        original = deepcopy(item['revision']['spec'])
        updated = deepcopy(original)
        updated['parameters'] = {'window': 30, 'parity_streak_threshold': 4}
        response = self.request('PATCH', base+f"/strategies/{item['id']}", json={
            'name': '生肖研究二号', 'spec': updated, 'expected_revision_id': item['active_revision_id']})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['item']['name'], '生肖研究二号')
        self.assertEqual(response.json['item']['revision']['spec']['parameters'], updated['parameters'])
        db.session.expire_all()
        self.assertEqual(json.loads(db.session.get(StrategyRevision, item['active_revision_id']).spec_json), original)
        self.assertEqual(response.json['item']['revision']['revision_no'], 2)
        bad = self.request('PATCH', base+f"/strategies/{item['id']}", json={
            'name': 'bad', 'spec': updated, 'expected_revision_id': item['active_revision_id']})
        self.assertEqual(bad.status_code, 409)
        self.login(self.member_user)
        self.assertEqual(self.request('PATCH', base+f"/strategies/{item['id']}", json={
            'name': 'no', 'spec': updated, 'expected_revision_id': response.json['item']['active_revision_id']}).status_code, 403)

    def test_name_only_draft_edit_does_not_publish_or_enqueue_backup(self):
        from models import StrategyDefinition, StrategyRevision, StrategyBackupDestination, StrategyBackupOutbox, db
        self.login(self.owner)
        base = f'/api/v2/workspaces/{self.space.id}'
        definition = StrategyDefinition(workspace_id=self.space.id, owner_user_id=self.owner.id,
            slug='imported-draft', name='原草稿', kind='rule_dsl', status='draft', visibility='private')
        db.session.add(definition)
        db.session.flush()
        revision = StrategyRevision(strategy_id=definition.id, revision_no=1, spec_json=json.dumps(NUMBER_SPEC), source='import')
        db.session.add_all([revision, StrategyBackupDestination(workspace_id=self.space.id, repository_ref='test/private', enabled=True)])
        db.session.commit()
        original_revision = revision.id
        response = self.request('PATCH', base+f'/strategies/{definition.public_id}', json={
            'name': '已重命名草稿', 'expected_revision_id': revision.id})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['item']['name'], '已重命名草稿')
        self.assertEqual(response.json['item']['status'], 'draft')
        self.assertIsNone(response.json['item']['active_revision_id'])
        self.assertEqual(response.json['item']['revision']['spec'], NUMBER_SPEC)
        self.assertEqual(StrategyBackupOutbox.query.count(), 0)
        self.assertEqual(json.loads(db.session.get(StrategyRevision, original_revision).spec_json), NUMBER_SPEC)
        self.assertEqual(response.json['item']['revision']['revision_no'], 2)

    def test_clone_rejects_invalid_names_without_truncation(self):
        self.login(self.owner)
        for name in (' ', 'x'*121, ['name']):
            response = self.request('POST', f'/api/v2/workspaces/{self.space.id}/builtin-strategies/zodiac_rebound/clone', json={'name': name})
            self.assertEqual(response.status_code, 400, response.json)

    def test_chat_session_identifies_strategy_and_current_rule(self):
        self.login(self.owner)
        base = f'/api/v2/workspaces/{self.space.id}'
        item = self.request('POST', base+'/builtin-strategies/zodiac_rebound/clone', json={'name': '编辑目标'}).json['item']
        response = self.request('POST', base+'/strategy-assistant/sessions', json={'strategy_id': item['id']})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json.get('strategy', {}).get('name'), '编辑目标')
        self.assertEqual(response.json['strategy']['revision_id'], item['active_revision_id'])


class ZodiacParameterTests(StrategyTestCase):
    def test_default_parameters_preserve_algorithm_and_custom_window_is_applied(self):
        from zodiac_rebound import build_prediction
        from strategy_platform.dsl import validate_strategy_spec
        from strategy_platform.service import execute_workspace_spec
        from strategy_platform.catalog import get_strategy_catalog
        original = build_prediction(example_history(), MAPPING)
        self.assertEqual(build_prediction(example_history(), MAPPING, window=20, parity_streak_threshold=5), original)
        changed = build_prediction(example_history(), MAPPING, window=10, parity_streak_threshold=6)
        self.assertEqual(changed['model_meta']['zodiac_rebound']['window'], 10)
        self.assertIsNone(changed['model_meta']['zodiac_rebound']['parity_streak']['preferred'])
        spec = {'schema_version': 1, 'kind': 'builtin_config', 'strategy_key': 'zodiac_rebound',
                'parameters': {'window': 10, 'parity_streak_threshold': 6}}
        validate_strategy_spec(spec)
        result = execute_workspace_spec(spec, example_history(), 'macau', '2026326', 'custom', number_to_zodiac=MAPPING)
        self.assertEqual(result['groups'][0]['items'], changed['model_meta']['zodiac_rebound']['six_zodiacs'])
        descriptor = next(row for row in get_strategy_catalog() if row['key'] == 'zodiac_rebound')
        self.assertTrue(descriptor['capabilities']['editable_parameters'])
        self.assertIn('parity_streak_threshold', descriptor['parameter_schema']['properties'])
        for parameters in ({'window': 9}, {'window': 241}, {'parity_streak_threshold': True}, {'parity_streak_threshold': 1}, {'pool': 9}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                validate_strategy_spec({**spec, 'parameters': parameters})

    def test_name_migration_updates_only_untouched_default(self):
        from strategy_platform.migrations import upgrade_strategy_schema
        with tempfile.TemporaryDirectory(prefix='workspace-name-migration-') as directory:
            engine = create_engine(f'sqlite:///{directory}/legacy.db')
            with engine.begin() as connection:
                connection.execute(text('CREATE TABLE strategy_schema_migrations(version INTEGER PRIMARY KEY, applied_at VARCHAR(40))'))
                connection.execute(text("INSERT INTO strategy_schema_migrations VALUES(1,'prior')"))
                connection.execute(text('CREATE TABLE strategy_workspaces(id INTEGER PRIMARY KEY, slug TEXT, name TEXT)'))
                connection.execute(text("INSERT INTO strategy_workspaces VALUES(1,'friends','朋友策略工作区'),(2,'custom','朋友策略工作区'),(3,'friends-copy','自定义名称')"))
            self.assertEqual(upgrade_strategy_schema(engine), [2])
            with engine.begin() as connection:
                self.assertEqual(connection.execute(text('SELECT name FROM strategy_workspaces WHERE id=1')).scalar(), '策略工作区')
                self.assertEqual(connection.execute(text('SELECT name FROM strategy_workspaces WHERE id=2')).scalar(), '朋友策略工作区')
                connection.execute(text("UPDATE strategy_workspaces SET name='我的名称' WHERE id=1"))
            self.assertEqual(upgrade_strategy_schema(engine), [])
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text('SELECT name FROM strategy_workspaces WHERE id=1')).scalar(), '我的名称')
            engine.dispose()
