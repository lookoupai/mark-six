from copy import deepcopy
import json
from unittest.mock import patch

from strategy_test_support import NUMBER_SPEC, StrategyTestCase
from test_zodiac_rebound import MAPPING, example_history


class StrategyExecutionTests(StrategyTestCase):
    def test_catalog_declares_executable_contracts_and_disabled_alias_is_rejected(self):
        from dataclasses import replace
        from strategy_platform.catalog import strategy_catalog, get_strategy_catalog
        for item in get_strategy_catalog():
            self.assertIn("execution_adapter_id", item)
            self.assertIn("parameter_schema", item)
            self.assertIn("per_user_subscription", item["capabilities"])
        user = self.user("catalog")
        self.login(user)
        descriptor = strategy_catalog.get("ai")
        with patch.dict(strategy_catalog._strategies, {"ai": replace(descriptor, enabled=False)}), \
                patch.object(self.module, "_get_prediction_data", side_effect=AssertionError("disabled strategy must not load data")):
            response = self.request("GET", "/api/predict?strategy=ai&region=macau")
        self.assertEqual(response.status_code, 400)

    def test_failed_run_is_recorded_and_can_retry_same_period(self):
        from models import StrategyRun
        owner = self.user("owner")
        space = self.workspace(owner)
        self.login(owner)
        base = f"/api/v2/workspaces/{space.id}/strategies"
        created = self.request("POST", base, json={"name": "retry", "spec": NUMBER_SPEC})
        url = f"{base}/{created.json['item']['id']}/runs"
        with patch.object(self.module, "_get_prediction_data", return_value=(example_history(), 2026)), \
                patch.object(self.module, "_get_next_period", return_value="2026326"), \
                patch("strategy_platform.api.execute_workspace_spec", side_effect=ValueError("private backend detail")):
            response = self.request("POST", url, json={"region": "macau"})
        self.assertEqual(response.status_code, 422)
        failed = StrategyRun.query.filter_by(workspace_id=space.id).one()
        self.assertEqual(failed.status, "failed")
        self.assertIsNotNone(failed.finished_at)
        self.assertEqual(failed.error_code, "strategy_execution_failed")
        self.assertNotIn("private backend detail", json.dumps(response.json))
        with patch.object(self.module, "_get_prediction_data", return_value=(example_history(), 2026)), \
                patch.object(self.module, "_get_next_period", return_value="2026326"):
            response = self.request("POST", url, json={"region": "macau"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StrategyRun.query.filter_by(workspace_id=space.id).count(), 1)
        self.assertEqual(StrategyRun.query.filter_by(workspace_id=space.id).one().status, "succeeded")

    def test_builtin_adapter_cannot_supplement_with_target_or_future_draws(self):
        from models import LotteryDraw, db
        from strategy_platform.service import execute_workspace_spec
        history = example_history()
        for draw in history:
            db.session.add(LotteryDraw(region="macau", draw_id=draw["id"], draw_date=draw["date"],
                                      normal_numbers=",".join(draw["no"]), special_number=draw["sno"]))
        db.session.commit()
        seen = []
        def predictor(key, data, region):
            loaded, _ = self.module._ensure_ml_prediction_history(data, region)
            seen.extend(draw["id"] for draw in loaded)
            return {"normal": [1, 2, 3, 4, 5, 6], "special": {"number": "7"}}
        spec = {"schema_version": 1, "kind": "builtin_config", "strategy_key": "ml",
                "parameters": {"window": 20}}
        with patch.object(self.module, "get_local_recommendations", side_effect=predictor):
            execute_workspace_spec(spec, history[10:], "macau", history[9]["id"], "test")
        self.assertTrue(seen)
        self.assertTrue(all(int(period) < int(history[9]["id"]) for period in seen), seen)
        self.assertEqual(self.module._current_backtest_cutoff_period(), "")

    def test_zodiac_clone_preserves_original_algorithm(self):
        from strategy_platform.service import execute_workspace_spec
        from zodiac_rebound import build_prediction
        owner = self.user("owner")
        space = self.workspace(owner)
        self.login(owner)
        response = self.request("POST", f"/api/v2/workspaces/{space.id}/builtin-strategies/zodiac_rebound/clone", json={})
        self.assertEqual(response.status_code, 201)
        spec = response.json["item"]["revision"]["spec"]
        result = execute_workspace_spec(spec, example_history(), "macau", "2026326", "clone",
                                        number_to_zodiac=MAPPING)
        expected = build_prediction(example_history(), MAPPING)["model_meta"]["zodiac_rebound"]
        actual = {group["id"]: group["items"] for group in result["groups"]}
        self.assertEqual(actual, {"six": expected["six_zodiacs"], "four": expected["four_zodiacs"]})

    def test_invalid_nested_spec_is_a_validation_error(self):
        from strategy_platform.dsl import validate_strategy_spec
        for fields in ({"regions": [{}]}, {"rank_metric": []}, {"rank_order": {}}, {"schema_version": True}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_strategy_spec({**NUMBER_SPEC, **fields})

    def test_source_rejects_duplicate_numbers(self):
        from models import MacauCollectedData, db
        from strategy_platform.sources import load_strategy_source_snapshot
        owner = self.user("owner")
        space = self.workspace(owner)
        db.session.add(MacauCollectedData(region="macau", period="2026001", numbers="1,1,1,1,1",
                                         zodiacs="鼠,牛,虎,兔,龙", year=2026, source_period="001"))
        db.session.commit()
        with self.assertRaises(ValueError):
            load_strategy_source_snapshot(space.id, "macau_five_zodiac", "macau", "2026001", owner.id)

    def test_settlement_infers_zodiac_and_has_independent_baselines(self):
        from strategy_platform import results
        self.assertTrue(hasattr(results, "evaluate_result"), "all result consumers need one evaluator")
        outcome = results.evaluate_result(
            {"kind": "zodiac_groups", "groups": [{"id": "six", "items": ["马", "蛇"]}]},
            {"id": "2026001", "sno": "1", "date": "2026-11-01"}, MAPPING)
        self.assertTrue(outcome["groups"]["six"]["hit"])
        self.assertEqual(outcome["groups"]["six"]["sample_count"], 1)
        self.assertEqual(outcome["groups"]["six"]["random_baseline"], 9 / 49)

    def test_external_source_without_timing_evidence_is_not_a_fair_sample(self):
        from strategy_platform import results
        self.assertTrue(hasattr(results, "evaluate_result"), "all result consumers need one evaluator")
        result = {"kind": "external_selection", "source": {"source_period": "2026001"},
                  "groups": [{"id": "numbers", "items": ["01", "02"]}]}
        draw = {"id": "2026001", "sno": "1", "date": "2026-11-01T12:00:00+00:00"}
        outcome = results.evaluate_result(result, draw, MAPPING)
        self.assertFalse(outcome["timing_verified"])
        self.assertEqual(outcome["groups"]["numbers"]["sample_count"], 0)
        result["source"]["published_at"] = "2026-11-01T13:00:00+02:00"
        self.assertTrue(results.evaluate_result(result, draw, MAPPING)["timing_verified"])
        result["source"]["published_at"] = "2026-11-01T13:00:00+00:00"
        self.assertFalse(results.evaluate_result(result, draw, MAPPING)["timing_verified"])
