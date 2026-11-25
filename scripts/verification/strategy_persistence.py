import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch
if os.environ.get('STRATEGY_VALIDATION_ONLY') != '1' or sys.argv[1:] not in (['seed'], ['check']):
    raise SystemExit('Use STRATEGY_VALIDATION_ONLY=1 with seed/check in a disposable container volume only')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
with patch('requests.sessions.Session.request', side_effect=AssertionError('network disabled')):
    import app
    from models import db, User, StrategyWorkspace, StrategyWorkspaceMembership, StrategyDefinition, StrategyRevision, StrategySubscription, StrategyRun
    with app.app.app_context():
        if sys.argv[1] == 'seed':
            user = User(username='persistence-fixture', email='fixture@example.invalid', password_hash='fixture-only', is_active=True, activation_expires_at=datetime.now()+timedelta(days=30))
            db.session.add(user)
            db.session.flush()
            workspace = StrategyWorkspace(slug='persistence-fixture', name='Persistence fixture')
            db.session.add(workspace)
            db.session.flush()
            db.session.add(StrategyWorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role='owner'))
            definition = StrategyDefinition(workspace_id=workspace.id, owner_user_id=user.id, slug='fixture-rule', name='Fixture rule', kind='rule_dsl', status='active')
            db.session.add(definition)
            db.session.flush()
            revision = StrategyRevision(strategy_id=definition.id, revision_no=1, spec_json='{"schema_version":1,"kind":"number_selection","window":20,"normal_count":6}', source='user')
            db.session.add(revision)
            db.session.flush()
            definition.active_revision_id = revision.id
            subscription = StrategySubscription(workspace_id=workspace.id, user_id=user.id, strategy_id=definition.id, run_mode='scheduled', enabled=True)
            db.session.add(subscription)
            db.session.flush()
            from strategy_platform.service import add_custom_strategy_run
            add_custom_strategy_run(workspace.id, definition, revision, user.id, 'macau', '2026326', '2026325', {'schema_version':1,'kind':'number_selection','selection':{'normal':[1,2,3,4,5,6],'special':{'number':'07'}}}, subscription.id)
            db.session.commit()
        user = User.query.filter_by(username='persistence-fixture').one()
        workspace = StrategyWorkspace.query.filter_by(slug='persistence-fixture').one()
        membership = StrategyWorkspaceMembership.query.filter_by(workspace_id=workspace.id,user_id=user.id).one()
        definition = StrategyDefinition.query.filter_by(workspace_id=workspace.id,slug='fixture-rule').one()
        revision = db.session.get(StrategyRevision,definition.active_revision_id)
        subscription = StrategySubscription.query.filter_by(user_id=user.id,strategy_id=definition.id).one()
        run = StrategyRun.query.filter_by(strategy_id=definition.id).one()
        assert membership.role == 'owner' and membership.status == 'active'
        assert revision.strategy_id == definition.id and revision.revision_no == 1
        assert subscription.enabled and run.subscription_id == subscription.id
        assert run.revision_id == revision.id and run.subject_user_id == user.id
        assert run.status == 'succeeded' and run.finished_at is not None
        assert len(workspace.public_id)==36
        print('PERSISTENCE PASS: account, role, definition, revision, subscription, result and links')
