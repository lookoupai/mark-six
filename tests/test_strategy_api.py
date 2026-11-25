from datetime import datetime, timedelta
import json

from strategy_test_support import NUMBER_SPEC, StrategyTestCase


class StrategyAccessTests(StrategyTestCase):
    def test_delete_author_preserves_run_and_detaches_subscription(self):
        from models import StrategyDefinition, StrategyRevision, StrategyRun, StrategySubscription, User, db
        from strategy_platform.service import add_custom_strategy_run
        from sqlalchemy import text
        admin = self.user("admin", admin=True)
        author = self.user("author")
        space = self.workspace(admin)
        self.member(space, author)
        self.login(author)
        created = self.request("POST", f"/api/v2/workspaces/{space.id}/strategies", json={"name": "retained", "spec": NUMBER_SPEC})
        definition = StrategyDefinition.query.filter_by(public_id=created.json["item"]["id"]).one()
        subscription = StrategySubscription(workspace_id=space.id, user_id=author.id, strategy_id=definition.id)
        db.session.add(subscription)
        db.session.flush()
        run = add_custom_strategy_run(space.id, definition, db.session.get(StrategyRevision, definition.active_revision_id),
              author.id, "macau", "2026326", "2026325", {"kind": "number_selection"}, subscription.id)
        db.session.commit()
        run_id, author_id = run.id, author.id
        if db.engine.dialect.name == "sqlite":
            db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.login(admin)
        deleted = self.request("DELETE", f"/admin/users/{author_id}/delete")
        self.assertEqual(deleted.status_code, 200, deleted.json)
        db.session.expire_all()
        self.assertIsNone(db.session.get(User, author_id))
        saved = db.session.get(StrategyRun, run_id)
        self.assertIsNone(saved.subscription_id)
        self.assertIsNone(saved.subject_user_id)
        self.assertEqual(db.session.get(StrategyDefinition, definition.id).owner_user_id, admin.id)

    def test_mobile_login_csrf_token_authorizes_v2_writes(self):
        from models import db
        user = self.user("mobile-author")
        user.set_password("Mobile-test-password9!")
        db.session.commit()
        space = self.workspace(user)
        response = self.client.post("/api/mobile/login", json={"username": user.username,
                                    "username_or_email": user.username, "password": "Mobile-test-password9!"})
        self.assertEqual(response.status_code, 200, response.json)
        token = response.json["csrf_token"]
        created = self.client.post(f"/api/v2/workspaces/{space.id}/strategies",
            json={"name": "mobile", "spec": NUMBER_SPEC}, headers={"X-CSRF-Token": token})
        self.assertEqual(created.status_code, 201, created.json)

    def test_subscription_migration_never_grants_or_reactivates_members(self):
        from models import StrategyWorkspaceMembership, StrategyAuditEvent, StrategySubscription, db
        from strategy_platform.legacy import migrate_legacy_auto_prediction_subscriptions
        from strategy_platform.service import seed_builtin_strategies
        owner = self.user("owner", admin=True)
        outsider = self.user("outsider")
        removed = self.user("removed")
        author = self.user("author")
        workspace = self.workspace(owner, "friends")
        self.member(workspace, removed, status="removed")
        self.member(workspace, author)
        seed_builtin_strategies()
        for _ in range(2):
            migrate_legacy_auto_prediction_subscriptions()
        self.assertIsNone(StrategyWorkspaceMembership.query.filter_by(user_id=outsider.id).first())
        self.assertEqual(StrategyWorkspaceMembership.query.filter_by(user_id=removed.id).one().status, "removed")
        self.assertEqual(StrategyWorkspaceMembership.query.filter_by(user_id=author.id).one().role, "strategy_author")
        self.assertEqual(StrategyAuditEvent.query.filter_by(action="member.grant").count(), 0)
        self.assertEqual(StrategySubscription.query.filter_by(user_id=outsider.id).count(), 0)
        self.assertGreater(StrategySubscription.query.filter_by(user_id=author.id).count(), 0)

    def test_disabled_workspace_cannot_be_accessed_by_direct_url(self):
        from models import db
        owner = self.user("owner")
        workspace = self.workspace(owner)
        workspace.status = "disabled"
        db.session.commit()
        self.login(owner)
        response = self.request("GET", f"/api/v2/workspaces/{workspace.id}/strategies")
        self.assertEqual(response.status_code, 403)

    def test_share_revoke_archive_and_owner_audit_flow(self):
        from models import StrategyAuditEvent, StrategySubscription
        owner, author, reader = self.user("owner"), self.user("author"), self.user("reader")
        space = self.workspace(owner)
        self.member(space, author)
        self.member(space, reader, "member")
        self.login(author)
        base = f"/api/v2/workspaces/{space.id}/strategies"
        created = self.request("POST", base, json={"name": "测试策略", "spec": NUMBER_SPEC})
        self.assertEqual(created.status_code, 201)
        strategy = created.json["item"]
        url = f"{base}/{strategy['id']}"
        self.request("POST", url + "/share", json={"visibility": "workspace"})
        self.login(reader)
        self.assertEqual(self.request("POST", url + "/subscriptions", json={"run_mode": "manual"}).status_code, 201)
        self.login(author)
        self.request("POST", url + "/share", json={"visibility": "private"})
        self.assertFalse(StrategySubscription.query.filter_by(user_id=reader.id).one().enabled)
        self.login(owner)
        self.assertEqual(self.request("GET", url).status_code, 200)
        self.assertGreater(StrategyAuditEvent.query.filter_by(action="strategy.owner_viewed").count(), 0)
        self.assertEqual(self.request("GET", url + "/export").status_code, 200)
        self.assertEqual(self.request("POST", url + "/archive", json={}).status_code, 400)
        self.assertEqual(self.request("POST", url + "/archive", json={"reason": "停止提供此策略"}).status_code, 200)
        self.assertEqual(self.request("POST", url + "/runs", json={"region": "hk"}).status_code, 404)

    def test_transfer_requires_current_owner_and_leaves_one_owner(self):
        from models import StrategyWorkspaceMembership
        owner, next_owner, other = self.user("owner"), self.user("next"), self.user("other")
        space = self.workspace(owner)
        self.member(space, next_owner)
        self.member(space, other)
        self.login(owner)
        url = f"/api/v2/workspaces/{space.id}/owner-transfer"
        self.assertEqual(self.request("POST", url, json={"user_id": next_owner.id}).status_code, 200)
        self.assertEqual(self.request("POST", url, json={"user_id": other.id}).status_code, 403)
        self.assertEqual(StrategyWorkspaceMembership.query.filter_by(workspace_id=space.id, role="owner", status="active").count(), 1)

    def test_json_body_must_be_object(self):
        owner = self.user("owner")
        space = self.workspace(owner)
        self.login(owner)
        response = self.request("POST", f"/api/v2/workspaces/{space.id}/strategies", json=[{"name": "bad"}])
        self.assertEqual(response.status_code, 400)

    def test_concurrent_owner_transfers_never_create_two_owners(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from models import StrategyWorkspaceMembership, db
        owner, first, second = self.user("owner"), self.user("first"), self.user("second")
        space = self.workspace(owner)
        self.member(space, first)
        self.member(space, second)
        owner_id, space_id, targets = owner.id, space.id, (first.id, second.id)
        db.session.remove()
        barrier = Barrier(2)
        def transfer(target):
            with self.app.test_client() as client:
                with client.session_transaction() as state:
                    state.update(user_id=owner_id, _csrf_token="concurrency-test")
                barrier.wait(timeout=5)
                return client.post(f"/api/v2/workspaces/{space_id}/owner-transfer", json={"user_id": target},
                                   headers={"X-CSRFToken": "concurrency-test"}).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            codes = list(executor.map(transfer, targets))
        self.assertEqual(codes.count(200), 1, codes)
        self.assertTrue(all(code in {200, 403, 409} for code in codes), codes)
        self.assertEqual(StrategyWorkspaceMembership.query.filter_by(workspace_id=space_id, role="owner", status="active").count(), 1)

    def test_private_proposal_backtest_is_excluded_from_public_summary(self):
        from models import BacktestRun, db
        from user import _latest_backtest_summary
        public = BacktestRun(name="public", region="hk", strategies="hot", periods_evaluated=1,
                            payload=json.dumps({"ranking": [{"strategy": "hot", "total": 1}]}))
        private = BacktestRun(name="private-secret-title", region="hk", strategies="rule_dsl", periods_evaluated=1,
                             payload="{}")
        db.session.add_all([public, private])
        db.session.commit()
        from unittest.mock import patch
        with patch("user._kickoff_backtest_snapshot_refresh"):
            rows = _latest_backtest_summary()
        self.assertNotIn("private-secret-title", [row["name"] for row in rows])


class ProposalLifecycleTests(StrategyTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.user("owner")
        self.author = self.user("author")
        self.space = self.workspace(self.owner)
        self.member(self.space, self.author)
        self.login(self.author)

    def proposal(self, status="backtested", expires_at=None):
        from models import StrategyChatSession, StrategyProposal, db
        chat = StrategyChatSession(workspace_id=self.space.id, user_id=self.author.id)
        db.session.add(chat)
        db.session.flush()
        row = StrategyProposal(session_id=chat.id, created_by_user_id=self.author.id, status=status,
                               proposed_spec_json=json.dumps({"name": "草案", "spec": NUMBER_SPEC,
                                                              "change_summary": "测试草案"}),
                               diff_json="{}", expires_at=expires_at)
        db.session.add(row)
        db.session.commit()
        return row

    def test_expired_proposal_cannot_publish(self):
        from models import StrategyDefinition
        row = self.proposal(expires_at=datetime.now() - timedelta(seconds=1))
        response = self.request("POST", f"/api/v2/workspaces/{self.space.id}/strategy-proposals/{row.id}/publish",
                                json={"confirm": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["error"], "proposal_expired")
        self.assertEqual(StrategyDefinition.query.filter(StrategyDefinition.workspace_id.is_not(None)).count(), 0)

    def test_published_proposal_cannot_backtest_again(self):
        row = self.proposal(status="published")
        response = self.request("POST", f"/api/v2/workspaces/{self.space.id}/strategy-proposals/{row.id}/backtests",
                                json={"region": "hk"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["error"], "proposal_terminal")

    def test_owner_cannot_publish_another_authors_private_proposal(self):
        row = self.proposal()
        self.login(self.owner)
        response = self.request("POST", f"/api/v2/workspaces/{self.space.id}/strategy-proposals/{row.id}/publish",
                                json={"confirm": True})
        self.assertEqual(response.status_code, 403)

    def test_publish_requires_explicit_confirmation_and_is_single_use(self):
        from models import StrategyDefinition, StrategyRevision
        row = self.proposal()
        url = f"/api/v2/workspaces/{self.space.id}/strategy-proposals/{row.id}/publish"
        self.assertEqual(self.request("POST", url, json={}).status_code, 400)
        self.assertEqual(self.request("POST", url, json={"confirm": True}).status_code, 200)
        self.assertEqual(self.request("POST", url, json={"confirm": True}).status_code, 409)
        definitions = StrategyDefinition.query.filter(StrategyDefinition.workspace_id.is_not(None)).all()
        self.assertEqual(len(definitions), 1)
        self.assertEqual(StrategyRevision.query.filter_by(strategy_id=definitions[0].id).count(), 1)
