"""Disposable, network-isolated fixtures for the strategy platform tests."""

import atexit
from datetime import datetime, timedelta
import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


_runtime_directory = None


def isolated_app():
    global _runtime_directory
    if _runtime_directory is None:
        _runtime_directory = tempfile.TemporaryDirectory(prefix="mark-six-strategy-tests-")
        atexit.register(_runtime_directory.cleanup)
    previous_cwd = os.getcwd()
    test_database = os.environ.get("STRATEGY_TEST_DATABASE_URL") or f"sqlite:///{_runtime_directory.name}/test.db"
    if not (test_database.startswith("sqlite:////tmp/") or (
        test_database.startswith("mysql+pymysql://validation:") and "@127.0.0.1:" in test_database
        and test_database.endswith("/mark_six_validation"))):
        raise AssertionError("only the disposable validation database is allowed")
    with patch.dict(os.environ, {
        "DATABASE_URL": test_database,
        "SECRET_KEY": "isolated-strategy-test-key", "ENABLE_SCHEDULER": "0",
        "ENABLE_STARTUP_BACKTEST_WARMUP": "0", "ENABLE_STARTUP_ML_WARMUP": "0",
        "ENABLE_STARTUP_ZODIAC_BACKFILL": "0", "NOTIFY_ASYNC": "0",
    }), patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network")):
        try:
            os.chdir(_runtime_directory.name)
            module = importlib.import_module("app")
        finally:
            os.chdir(previous_cwd)
    database = module.app.config["SQLALCHEMY_DATABASE_URI"]
    if database != test_database:
        raise AssertionError("strategy tests cannot reuse an unrelated application database")
    module.app.config.update(TESTING=True)
    return module


class StrategyTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = isolated_app()
        cls.app = cls.module.app

    def setUp(self):
        from models import db
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        # Each disposable DB reuses numeric user IDs, but rate limits live in
        # process memory. Isolate the buckets while exercising the real limiter.
        self.rate_buckets = patch.dict('rate_limiter._BUCKETS', {}, clear=True)
        self.rate_buckets.start()
        self.addCleanup(self.rate_buckets.stop)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        db.session.remove()
        db.drop_all()
        db.create_all()
        from strategy_platform.service import seed_builtin_strategies
        seed_builtin_strategies()
        self.addCleanup(db.engine.dispose)
        self.addCleanup(db.session.remove)
        self.client = self.app.test_client()

    def user(self, name, admin=False):
        from models import User, db
        user = User(username=name, email=f"{name}@example.invalid", password_hash="test-only",
                    is_active=True, is_admin=admin, activation_expires_at=datetime.now() + timedelta(days=30))
        db.session.add(user)
        db.session.commit()
        return user

    def workspace(self, owner, slug="test-workspace"):
        from models import StrategyWorkspace, StrategyWorkspaceMembership, db
        workspace = StrategyWorkspace(slug=slug, name="测试工作区", status="active")
        db.session.add(workspace)
        db.session.flush()
        db.session.add(StrategyWorkspaceMembership(workspace_id=workspace.id, user_id=owner.id,
                                                  role="owner", status="active"))
        db.session.commit()
        return workspace

    def member(self, workspace, user, role="strategy_author", status="active"):
        from models import StrategyWorkspaceMembership, db
        row = StrategyWorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=role, status=status)
        db.session.add(row)
        db.session.commit()
        return row

    def login(self, user):
        with self.client.session_transaction() as session:
            session.update(user_id=user.id, username=user.username, is_active=True,
                           is_admin=user.is_admin, _csrf_token="strategy-test-csrf")

    def request(self, method, path, **kwargs):
        kwargs.setdefault("headers", {})["X-CSRFToken"] = "strategy-test-csrf"
        return self.client.open(path, method=method, **kwargs)


NUMBER_SPEC = {"schema_version": 1, "kind": "number_selection", "regions": ["hk", "macau"],
               "window": 20, "normal_count": 6, "rank_metric": "frequency", "rank_order": "highest"}
