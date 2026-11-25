from datetime import datetime, timedelta
import importlib.util
import json
import tempfile

from sqlalchemy import create_engine, inspect, text
from unittest.mock import patch

from strategy_test_support import NUMBER_SPEC, StrategyTestCase


class StrategyMigrationTests(StrategyTestCase):
    def test_sqlite_failed_migration_is_atomic_and_retryable(self):
        from strategy_platform.migrations import upgrade_strategy_schema
        with tempfile.TemporaryDirectory(prefix="mark-six-schema-failure-") as directory:
            engine = create_engine(f"sqlite:///{directory}/old.db")
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE strategy_backup_outbox (id INTEGER PRIMARY KEY, destination_id INTEGER, revision_id INTEGER, CONSTRAINT uq_strategy_backup_revision_destination UNIQUE(destination_id,revision_id))"))
            with patch("strategy_platform.migrations._upgrade_outbox_unique", side_effect=RuntimeError("simulated crash")):
                with self.assertRaises(RuntimeError):
                    upgrade_strategy_schema(engine)
            self.assertNotIn("event_type", [c["name"] for c in inspect(engine).get_columns("strategy_backup_outbox")])
            upgrade_strategy_schema(engine)
            self.assertIn("event_type", [c["name"] for c in inspect(engine).get_columns("strategy_backup_outbox")])
            engine.dispose()

    def test_versioned_upgrade_of_existing_workspace_and_outbox_is_repeatable(self):
        self.assertIsNotNone(importlib.util.find_spec("strategy_platform.migrations"))
        from strategy_platform.migrations import upgrade_strategy_schema
        with tempfile.TemporaryDirectory(prefix="mark-six-schema-test-") as directory:
            engine = create_engine(f"sqlite:///{directory}/legacy.db")
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE strategy_workspaces (id INTEGER PRIMARY KEY, slug VARCHAR(80))"))
                connection.execute(text("INSERT INTO strategy_workspaces (id,slug) VALUES (1,'legacy')"))
                connection.execute(text("CREATE TABLE strategy_backup_outbox (id INTEGER PRIMARY KEY, destination_id INTEGER, revision_id INTEGER, CONSTRAINT uq_strategy_backup_revision_destination UNIQUE (destination_id,revision_id))"))
                connection.execute(text("INSERT INTO strategy_backup_outbox (id,destination_id,revision_id) VALUES (1,2,3)"))
            upgrade_strategy_schema(engine)
            upgrade_strategy_schema(engine)
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM strategy_schema_migrations")).scalar(), 2)
                self.assertEqual(len(connection.execute(text("SELECT public_id FROM strategy_workspaces")).scalar()), 36)
                self.assertEqual(connection.execute(text("SELECT event_type FROM strategy_backup_outbox")).scalar(), "revision")
            engine.dispose()

    def test_full_export_restore_respects_foreign_keys_and_proposal_links(self):
        from admin import _build_data_export_payload, _import_data_payload
        from models import StrategyChatSession, StrategyProposal, StrategyDefinition, db
        owner = self.user("owner", admin=True)
        space = self.workspace(owner)
        self.login(owner)
        response = self.request("POST", f"/api/v2/workspaces/{space.id}/strategies", json={"name": "roundtrip", "spec": NUMBER_SPEC})
        definition = StrategyDefinition.query.filter_by(public_id=response.json["item"]["id"]).one()
        chat = StrategyChatSession(workspace_id=space.id, user_id=owner.id, strategy_id=definition.id)
        db.session.add(chat)
        db.session.flush()
        db.session.add(StrategyProposal(session_id=chat.id, strategy_id=definition.id, base_revision_id=definition.active_revision_id,
                                       created_by_user_id=owner.id, proposed_spec_json="{}", diff_json="{}"))
        db.session.commit()
        if db.engine.dialect.name == "sqlite":
            db.session.execute(text("PRAGMA foreign_keys=ON"))
        exported = _build_data_export_payload()
        _import_data_payload(exported, "replace")
        self.assertEqual(StrategyChatSession.query.one().strategy_id, definition.id)
        self.assertEqual(StrategyProposal.query.one().base_revision_id, definition.active_revision_id)

    def test_retention_releases_expired_backtest_reference(self):
        from models import BacktestRun, StrategyChatSession, StrategyProposal, db
        from retention_service import cleanup_expired_data
        owner = self.user("owner")
        space = self.workspace(owner)
        chat = StrategyChatSession(workspace_id=space.id, user_id=owner.id)
        backtest = BacktestRun(name="old", created_at=datetime.now() - timedelta(days=100))
        db.session.add_all([chat, backtest])
        db.session.flush()
        proposal = StrategyProposal(session_id=chat.id, backtest_run_id=backtest.id,
                                    proposed_spec_json="{}", diff_json="{}", status="backtested")
        db.session.add(proposal)
        db.session.commit()
        if db.engine.dialect.name == "sqlite":
            db.session.execute(text("PRAGMA foreign_keys=ON"))
        cleanup_expired_data()
        db.session.expire_all()
        self.assertIsNone(proposal.backtest_run_id)
        self.assertEqual(BacktestRun.query.count(), 0)
