import base64
from copy import deepcopy
import json
from unittest.mock import Mock, patch

from strategy_test_support import NUMBER_SPEC, StrategyTestCase


class StrategyBackupTests(StrategyTestCase):
    def setUp(self):
        super().setUp()
        from models import StrategyBackupDestination, db
        self.owner = self.user("owner")
        self.space = self.workspace(self.owner)
        self.login(self.owner)
        self.destination = StrategyBackupDestination(workspace_id=self.space.id, repository_ref="test/private",
                                                      enabled=True, branch="main")
        db.session.add(self.destination)
        db.session.commit()
        created = self.request("POST", f"/api/v2/workspaces/{self.space.id}/strategies",
                               json={"name": "公开规则名称", "spec": NUMBER_SPEC})
        self.assertEqual(created.status_code, 201)
        self.strategy = created.json["item"]

    def response(self, status, payload):
        return Mock(status_code=status, content=b"json", json=Mock(return_value=payload))

    def test_repository_rate_limit_keeps_published_strategy_and_retries(self):
        from models import StrategyBackupOutbox, StrategyDefinition
        from strategy_platform.backup import process_strategy_backup_outbox
        for status in (429, 503, 401):
            with self.subTest(status=status), patch.dict("os.environ", {"STRATEGY_GITHUB_TOKEN": "test-token"}), \
                    patch("strategy_platform.backup.requests.get", return_value=self.response(status, {})):
                row = StrategyBackupOutbox.query.one()
                row.retry_at = None
                row.status = "pending"
                process_strategy_backup_outbox()
                self.assertEqual(row.status, "pending")
                self.assertIsNotNone(row.retry_at)
                self.assertEqual(StrategyDefinition.query.get(row.strategy_id).status, "active")

    def test_existing_file_must_match_before_marking_synced(self):
        from models import StrategyBackupOutbox
        from strategy_platform.backup import process_strategy_backup_outbox
        repository = self.response(200, {"private": True})
        wrong_file = self.response(200, {"encoding": "base64", "content": base64.b64encode(b"wrong").decode()})
        with patch.dict("os.environ", {"STRATEGY_GITHUB_TOKEN": "test-token"}), \
                patch("strategy_platform.backup.requests.get", side_effect=[repository, wrong_file]), \
                patch("strategy_platform.backup.requests.put", return_value=self.response(422, {"message": "sha wasn't supplied"})):
            process_strategy_backup_outbox()
        self.assertNotEqual(StrategyBackupOutbox.query.one().status, "synced")

    def test_bundle_roundtrip_requires_digest_and_creates_draft_only(self):
        from models import StrategyDefinition, StrategySubscription
        base = f"/api/v2/workspaces/{self.space.id}"
        response = self.request("GET", f"{base}/strategies/{self.strategy['id']}/export")
        bundle = response.json["bundle"]
        self.assertIn("digest", bundle)
        self.assertNotIn("workspace", bundle)
        preview = self.request("POST", base + "/management/backup/import-preview", json={"bundle": bundle})
        self.assertEqual(preview.status_code, 200)
        damaged = deepcopy(bundle)
        damaged["strategy"]["spec"]["window"] = 99
        self.assertEqual(self.request("POST", base + "/management/backup/import", json={"bundle": damaged,
                         "expected_digest": bundle["digest"], "confirm": True}).status_code, 422)
        result = self.request("POST", base + "/management/backup/import", json={"bundle": bundle,
                              "expected_digest": preview.json["digest"], "confirm": True})
        self.assertEqual(result.status_code, 201)
        definition = StrategyDefinition.query.filter_by(public_id=result.json["item"]["id"]).one()
        self.assertEqual(definition.status, "draft")
        self.assertIsNone(definition.active_revision_id)
        self.assertEqual(StrategySubscription.query.count(), 0)

    def test_backup_content_is_immutable_across_retries_and_definition_rename(self):
        from models import StrategyBackupOutbox, StrategyDefinition, db
        from strategy_platform.backup import process_strategy_backup_outbox
        sent = []
        def upload(*args, **kwargs):
            sent.append(kwargs["json"]["content"])
            return self.response(503, {})
        with patch.dict("os.environ", {"STRATEGY_GITHUB_TOKEN": "test-token"}), \
                patch("strategy_platform.backup.requests.get", return_value=self.response(200, {"private": True})), \
                patch("strategy_platform.backup.requests.put", side_effect=upload):
            process_strategy_backup_outbox()
            row = StrategyBackupOutbox.query.one()
            definition = StrategyDefinition.query.get(row.strategy_id)
            definition.name = "different title"
            row.retry_at = None
            db.session.commit()
            process_strategy_backup_outbox()
        self.assertEqual(sent[0], sent[1])

    def test_secret_in_rule_label_never_leaves_strategy_database(self):
        from models import StrategyBackupOutbox, StrategyDefinition
        spec = {"schema_version": 1, "kind": "zodiac_groups", "window": 20,
                "groups": [{"id": "six", "label": "ghp_" + "x" * 30, "size": 6}]}
        response = self.request("POST", f"/api/v2/workspaces/{self.space.id}/strategies",
                                json={"name": "private label", "spec": spec})
        self.assertEqual(response.status_code, 201)
        definition = StrategyDefinition.query.filter_by(public_id=response.json["item"]["id"]).one()
        self.assertEqual(definition.status, "active")
        outbox = StrategyBackupOutbox.query.filter_by(strategy_id=definition.id).one()
        self.assertEqual(outbox.status, "failed")
        self.assertEqual(outbox.last_error_code, "backup_sensitive_content")
        self.assertIsNone(outbox.payload_json)
        exported = self.request("GET", f"/api/v2/workspaces/{self.space.id}/strategies/{definition.public_id}/export")
        self.assertEqual(exported.status_code, 422)

    def test_archive_queues_separate_tombstone(self):
        from models import StrategyBackupOutbox
        response = self.request("POST", f"/api/v2/workspaces/{self.space.id}/strategies/{self.strategy['id']}/archive",
                                json={"reason": "归档"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StrategyBackupOutbox.query.count(), 2)
        tombstone = StrategyBackupOutbox.query.filter_by(event_type="tombstone").one()
        self.assertEqual(json.loads(tombstone.payload_json)["status"], "archived")

    def test_outbox_commits_each_result_before_next_remote_request(self):
        from models import StrategyBackupOutbox
        from strategy_platform.backup import process_strategy_backup_outbox
        self.request("POST", f"/api/v2/workspaces/{self.space.id}/strategies",
                     json={"name": "another", "spec": NUMBER_SPEC})
        observed = []
        def delivery(row):
            from models import db
            if observed:
                with db.engine.connect() as connection:
                    status = connection.execute(StrategyBackupOutbox.__table__.select().where(
                        StrategyBackupOutbox.id == observed[0])).mappings().one()["status"]
                    self.assertEqual(status, "synced")
            row.status = "synced"
            observed.append(row.id)
        with patch("strategy_platform.backup._deliver", side_effect=delivery):
            process_strategy_backup_outbox()
        self.assertEqual(len(observed), 2)
