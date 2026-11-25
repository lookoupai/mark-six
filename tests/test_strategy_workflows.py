from copy import deepcopy
from datetime import datetime
import json
from unittest.mock import Mock, patch

from strategy_test_support import NUMBER_SPEC, StrategyTestCase
from test_zodiac_rebound import MAPPING, example_history


class StrategyWorkflowTests(StrategyTestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.author, self.member_user = self.user("owner"), self.user("author"), self.user("reader")
        self.space = self.workspace(self.owner)
        self.member(self.space, self.author)
        self.member(self.space, self.member_user, "member")
        self.base = f"/api/v2/workspaces/{self.space.id}"
        self.login(self.author)

    def send_proposal(self, spec=NUMBER_SPEC, strategy_id=None):
        session = self.request("POST", self.base + "/strategy-assistant/sessions", json={"strategy_id": strategy_id})
        self.assertEqual(session.status_code, 201)
        session_url = self.base + f"/strategy-assistant/sessions/{session.json['session_id']}"
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": json.dumps({
            "reply": "请检查草案后回测", "proposal": {"name": "可回测规则", "change_summary": "历史频率", "spec": spec}})}}]}
        with patch.object(self.module, "get_ai_config", return_value={"api_key": "test-key", "api_url": "https://example.invalid", "model": "mock"}), \
                patch("strategy_platform.api.requests.post", return_value=response):
            proposal = self.request("POST", session_url + "/messages", json={"message": "创建一个频率规则"})
        self.assertEqual(proposal.status_code, 200)
        return proposal.json, session_url

    def store_history(self):
        from models import LotteryDraw, db
        for draw in example_history():
            db.session.add(LotteryDraw(region="macau", draw_id=draw["id"], draw_date=draw["date"],
                                      normal_numbers=",".join(draw["no"]), special_number=draw["sno"],
                                      special_zodiac=draw["sno_zodiac"], raw_zodiac=draw["raw_zodiac"]))
        db.session.commit()

    def test_assistant_to_backtest_publish_run_subscribe_settle(self):
        from models import StrategyDefinition, StrategyRun
        self.store_history()
        proposal, chat_url = self.send_proposal()
        self.assertEqual(StrategyDefinition.query.filter_by(workspace_id=self.space.id).count(), 0)
        self.assertEqual(self.request("GET", chat_url).json["proposals"][0]["status"], "validated")
        url = self.base + f"/strategy-proposals/{proposal['proposal_id']}"
        with patch.object(self.module, "_get_number_to_zodiac_map", return_value=MAPPING):
            backtest = self.request("POST", url + "/backtests", json={"region": "macau"})
        self.assertEqual(backtest.status_code, 200, backtest.json)
        self.assertGreater(backtest.json["backtest"]["summary"]["special"]["total"], 0)
        for row in backtest.json["backtest"]["results"]:
            self.assertLess(int(row["input_cutoff"]), int(row["period"]))
        published = self.request("POST", url + "/publish", json={"confirm": True})
        self.assertEqual(published.status_code, 200)
        strategy_url = self.base + f"/strategies/{published.json['item']['id']}"
        with patch.object(self.module, "_get_prediction_data", return_value=(example_history(), 2026)), \
                patch.object(self.module, "_get_next_period", return_value="2026326"):
            ran = self.request("POST", strategy_url + "/runs", json={"region": "macau"})
            reran = self.request("POST", strategy_url + "/runs", json={"region": "macau"})
        self.assertEqual(ran.status_code, 200)
        self.assertEqual(ran.json["run_id"], reran.json["run_id"])
        self.assertEqual(self.request("POST", strategy_url + "/subscriptions", json={"run_mode": "scheduled", "regions": ["macau"]}).status_code, 201)
        self.assertEqual(self.request("DELETE", strategy_url + "/subscriptions").status_code, 200)
        target = {**example_history()[0], "id": "2026326", "date": "2026-11-22"}
        with patch.object(self.module, "_get_number_to_zodiac_map", return_value=MAPPING):
            self.module.update_prediction_accuracy([target], "macau", trigger_auto_predictions=False, tune_strategy_configs=False)
        run = StrategyRun.query.get(ran.json["run_id"])
        self.assertIsNotNone(run.settlement_json)
        history = self.request("GET", strategy_url + "/runs")
        self.assertEqual(history.json["items"][0]["settlement"]["sample_count"], 1)

    def test_assistant_rejects_unsafe_spec_and_preserves_private_session(self):
        bad = {**NUMBER_SPEC, "python": "print('unsafe')"}
        proposal, chat_url = self.send_proposal(bad)
        self.assertIsNone(proposal["proposal_id"])
        self.assertFalse(proposal["validation"]["valid"])
        self.login(self.owner)
        self.assertEqual(self.request("GET", chat_url).status_code, 403)
        self.login(self.member_user)
        self.assertEqual(self.request("POST", self.base + "/strategy-assistant/sessions", json={}).status_code, 403)
        self.login(self.author)
        with patch("strategy_platform.api.rate_limited", return_value=True):
            self.assertEqual(self.request("POST", chat_url + "/messages", json={"message": "hello"}).status_code, 429)

    def test_membership_management_and_multitenant_isolation(self):
        outsider = self.user("outsider")
        foreign = self.workspace(outsider, "elsewhere")
        self.assertEqual(self.request("GET", f"/api/v2/workspaces/{foreign.id}/strategies").status_code, 403)
        self.login(self.owner)
        self.assertEqual(self.request("POST", self.base + "/members", json={"username": outsider.username, "role": "strategy_author"}).status_code, 201)
        self.assertEqual(self.request("PATCH", self.base + f"/members/{outsider.id}", json={"role": "member"}).status_code, 200)
        self.assertEqual(len(self.request("GET", self.base + "/members").json["items"]), 4)
        self.assertEqual(self.request("DELETE", self.base + f"/members/{outsider.id}").status_code, 200)
        self.login(outsider)
        self.assertEqual(self.request("GET", self.base + "/strategies").status_code, 403)
        workspaces = self.request("GET", "/api/v2/workspaces").json["items"]
        self.assertEqual([item["id"] for item in workspaces], [foreign.id])

    def test_source_versions_append_and_latest_snapshot_is_reused(self):
        from models import MacauCollectedData, StrategySourceSnapshot, db
        from strategy_platform.sources import capture_macau_collection_snapshots, load_strategy_source_snapshot
        record = MacauCollectedData(region="macau", period="2026326", year=2026, source_period="326",
                                    numbers="1,2,3,4,5", zodiacs="鼠,牛,虎,兔,龙")
        db.session.add(record)
        db.session.flush()
        self.assertEqual(capture_macau_collection_snapshots([record]), 1)
        db.session.commit()
        first = load_strategy_source_snapshot(self.space.id, "macau_five_zodiac", "macau", "2026326", self.author.id)
        self.assertEqual(capture_macau_collection_snapshots([record]), 0)
        record.numbers = "2,3,4,5,6"
        record.updated_at = datetime.now()
        self.assertEqual(capture_macau_collection_snapshots([record]), 1)
        db.session.commit()
        latest = load_strategy_source_snapshot(self.space.id, "macau_five_zodiac", "macau", "2026326", self.author.id)
        self.assertNotEqual(first["id"], latest["id"])
        self.assertEqual(StrategySourceSnapshot.query.count(), 2)

    def test_scheduled_run_respects_revocation_and_reuses_period_result(self):
        from models import StrategyRun, db
        self.login(self.author)
        created = self.request("POST", self.base + "/strategies", json={"name": "auto", "spec": NUMBER_SPEC})
        url = self.base + f"/strategies/{created.json['item']['id']}"
        self.request("POST", url + "/subscriptions", json={"run_mode": "scheduled", "regions": ["macau"]})
        with patch.object(self.module, "notify_user") as notify:
            self.module._generate_subscribed_custom_predictions(example_history(), "macau", "2026326")
            self.module._generate_subscribed_custom_predictions(example_history(), "macau", "2026326")
        self.assertEqual(StrategyRun.query.filter_by(workspace_id=self.space.id).count(), 1)
        self.assertEqual(notify.call_count, 1)
        self.space.status = "disabled"
        db.session.commit()
        with patch.object(self.module, "notify_user") as notify:
            self.module._generate_subscribed_custom_predictions(example_history(), "macau", "2026327")
        notify.assert_not_called()
        self.assertEqual(StrategyRun.query.filter_by(workspace_id=self.space.id).count(), 1)

    def test_shared_clone_is_independent_and_revision_conflict_preserves_active(self):
        from models import StrategyRevision
        created = self.request("POST", self.base + "/strategies", json={"name": "source", "spec": NUMBER_SPEC})
        item = created.json["item"]
        url = self.base + f"/strategies/{item['id']}"
        self.request("POST", url + "/share", json={"visibility": "workspace"})
        self.login(self.owner)
        cloned = self.request("POST", url + "/clone", json={"name": "own-copy"})
        self.assertEqual(cloned.status_code, 201)
        clone_url = self.base + f"/strategies/{cloned.json['item']['id']}"
        changed = {**NUMBER_SPEC, "window": 10}
        revision_id = cloned.json["item"]["active_revision_id"]
        self.assertEqual(self.request("PATCH", clone_url, json={"spec": changed, "expected_revision_id": revision_id}).status_code, 200)
        self.assertEqual(self.request("PATCH", clone_url, json={"spec": NUMBER_SPEC, "expected_revision_id": revision_id}).status_code, 409)
        self.assertEqual(self.request("GET", url).json["item"]["revision"]["spec"]["window"], 20)
        self.assertEqual(self.request("GET", clone_url).json["item"]["revision"]["spec"]["window"], 10)

    def test_dsl_zodiac_groups_use_historical_mapping_and_repeat_deterministically(self):
        from strategy_platform.service import execute_workspace_spec
        spec = {"schema_version": 1, "kind": "zodiac_groups", "window": 20, "groups": [
            {"id": "frequency", "size": 6, "metric": "frequency", "order": "highest"},
            {"id": "special", "size": 4, "metric": "special_frequency", "order": "lowest"}]}
        history = example_history()
        result = execute_workspace_spec(spec, history, "macau", "2026326", "fixture", number_to_zodiac=MAPPING)
        reversed_result = execute_workspace_spec(spec, list(reversed(history)), "macau", "2026326", "fixture", number_to_zodiac=MAPPING)
        self.assertEqual(result, reversed_result)
        self.assertEqual([len(group["items"]) for group in result["groups"]], [6, 4])

    def test_ai_provider_failures_and_unconfigured_state_do_not_publish(self):
        from models import StrategyDefinition
        session = self.request("POST", self.base + "/strategy-assistant/sessions", json={}).json["session_id"]
        url = self.base + f"/strategy-assistant/sessions/{session}/messages"
        with patch.object(self.module, "get_ai_config", return_value={}):
            self.assertEqual(self.request("POST", url, json={"message": "idea"}).status_code, 400)
        with patch.object(self.module, "get_ai_config", return_value={"api_key": "test", "api_url": "https://example.invalid"}), \
                patch("strategy_platform.api.requests.post", side_effect=RuntimeError("sensitive provider detail")):
            response = self.request("POST", url, json={"message": "idea"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("sensitive provider detail", response.get_data(as_text=True))
        self.assertEqual(StrategyDefinition.query.filter_by(workspace_id=self.space.id).count(), 0)
