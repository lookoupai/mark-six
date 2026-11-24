"""Run: python -m unittest discover -s tests -v (uses an isolated SQLite DB)."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
import importlib
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from zodiac_rebound import build_prediction, evaluate_groups, summarize_groups, ZODIACS


MAPPING = {n: "马蛇龙兔虎牛鼠猪狗鸡猴羊"[(n - 1) % 12] for n in range(1, 50)}


def example_history():
    """Synthetic complete draws matching the supplied example's key statistics.

    These are constructed test data, not actual period 325 lottery results.
    """
    targets = dict(zip(ZODIACS, [12, 4, 12, 13, 12, 15, 12, 11, 11, 16, 12, 10]))
    specials = [11, 7, 5, 3, 11, 10, 2, 8, 12, 5, 3, 7, 11, 2, 10, 3, 1, 5, 6, 7]
    remaining = Counter(targets)
    remaining.subtract(MAPPING[n] for n in specials)
    latest = [37, 1, 10, 4, 9, 3]
    remaining.subtract(MAPPING[n] for n in latest)
    history = []
    for i, special in enumerate(specials):
        normal = list(latest) if i == 0 else []
        while len(normal) < 6:
            available = [n for n in range(1, 50) if n not in normal + [special]
                         and remaining[MAPPING[n]] > 0]
            number = max(available, key=lambda n: (remaining[MAPPING[n]], -n))
            normal.append(number)
            remaining[MAPPING[number]] -= 1
        history.append({"id": str(2026325 - i),
                        "date": (datetime(2026, 11, 21) - timedelta(days=i)).strftime("%Y-%m-%d"),
                        "no": [str(n) for n in normal], "sno": str(special),
                        "sno_zodiac": MAPPING[special],
                        "raw_zodiac": ",".join(MAPPING[n] for n in normal + [special])})
    assert not any(remaining.values()), remaining
    return history


class ZodiacRuleTests(unittest.TestCase):
    def setUp(self):
        self.history = example_history()
        self.result = build_prediction(self.history, MAPPING)
        self.analysis = self.result["model_meta"]["zodiac_rebound"]

    def test_reference_scenario(self):
        self.assertEqual(self.analysis["six_zodiacs"], ["兔", "狗", "牛", "马", "鸡", "蛇"])
        self.assertEqual(self.analysis["four_zodiacs"], ["兔", "狗", "马", "鸡"])
        rows = {r["zodiac"]: r for r in self.analysis["statistics"]}
        self.assertEqual(rows["鸡"]["total_count"], 16)
        self.assertEqual(rows["蛇"]["total_count"], 15)
        self.assertEqual(rows["马"]["latest_normal_count"], 2)
        self.assertEqual(rows["牛"]["omission"], 18)
        self.assertEqual(rows["马"]["omission"], 16)
        self.assertTrue(rows["兔"]["omission_lower_bound"])
        self.assertEqual(self.analysis["parity_streak"]["length"], 5)
        self.assertEqual(self.analysis["parity_streak"]["preferred"], "even")
        self.assertTrue(self.analysis["balanced_four"])
        self.assertEqual(sum(r["total_count"] for r in rows.values()), 140)
        self.assertEqual(sum(r["special_count"] for r in rows.values()), 20)

    def test_order_duplicates_determinism_and_no_mutation(self):
        original = deepcopy(self.history)
        actual = build_prediction(list(reversed(self.history)) + [self.history[0]], MAPPING)
        self.assertEqual(actual, self.result)
        self.assertEqual(self.history, original)
        conflicting = deepcopy(self.history[0])
        conflicting["sno"] = "23"
        with self.assertRaisesRegex(ValueError, "冲突"):
            build_prediction(self.history + [conflicting], MAPPING)

    def test_omission_looks_past_frequency_window(self):
        old = deepcopy(self.history[-1])
        old.update(id="2026305", date="2026-11-01", sno="40", sno_zodiac="兔")
        old["no"] = ["1", "2", "3", "5", "6", "7"]
        old["raw_zodiac"] = "马,蛇,龙,虎,牛,鼠,兔"
        result = build_prediction(self.history + [old], MAPPING)
        rows = {r["zodiac"]: r for r in result["model_meta"]["zodiac_rebound"]["statistics"]}
        self.assertEqual(rows["兔"]["omission"], 20)
        self.assertFalse(rows["兔"]["omission_lower_bound"])
        self.assertEqual(rows["狗"]["omission"], 21)
        self.assertTrue(rows["狗"]["omission_lower_bound"])
        self.assertEqual(rows["鸡"]["total_count"], 16)

    def test_invalid_and_short_data_do_not_fabricate_results(self):
        for count in (0, 1, 19):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "至少 20"):
                build_prediction(self.history[:count], MAPPING)
        for value in ("50", "0", "x", None, "1"):
            invalid = deepcopy(self.history)
            invalid[0]["sno"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_prediction(invalid, MAPPING)
        with self.assertRaisesRegex(ValueError, "映射不完整"):
            build_prediction(self.history, {1: "马"})

    def test_date_specific_fallback_and_source_zodiacs(self):
        missing = deepcopy(self.history)
        for draw in missing:
            draw.pop("raw_zodiac")
            draw.pop("sno_zodiac")
        dates = []

        def mapping(draw):
            dates.append(draw["date"])
            return MAPPING

        self.assertEqual(build_prediction(missing, MAPPING, mapping), self.result)
        self.assertEqual(len(dates), 20)
        traditional = deepcopy(self.history)
        for draw in traditional:
            draw["raw_zodiac"] = draw["raw_zodiac"].translate(str.maketrans("龙马鸡猪", "龍馬雞豬"))
        self.assertEqual(build_prediction(traditional, MAPPING), self.result)

    def test_year_specific_parity_and_variable_random_baseline(self):
        next_mapping = {n: "羊马蛇龙兔虎牛鼠猪狗鸡猴"[(n - 1) % 12] for n in range(1, 50)}
        next_result = build_prediction(self.history, next_mapping)
        analysis = next_result["model_meta"]["zodiac_rebound"]
        rows = {r["zodiac"]: r for r in analysis["statistics"]}
        self.assertEqual(rows["马"]["parity"], "even")
        self.assertEqual(self.analysis["six_random_baseline"], 25 / 49)
        self.assertEqual(analysis["six_random_baseline"], 24 / 49)
        self.assertTrue(set(analysis["four_zodiacs"]) <= set(analysis["six_zodiacs"]))

    def test_unbalanced_pool_degrades_explicitly(self):
        custom = dict(MAPPING)
        selected = set(self.analysis["six_zodiacs"])
        odd_numbers = iter(range(1, 50, 2))
        even_numbers = iter(range(2, 50, 2))
        for z in ZODIACS:
            iterator = odd_numbers if z in selected else even_numbers
            for _ in range(4):
                custom[next(iterator)] = z
        custom[49] = "兔"
        analysis = build_prediction(self.history, custom)["model_meta"]["zodiac_rebound"]
        self.assertEqual(len(analysis["four_zodiacs"]), 4)
        self.assertFalse(analysis["balanced_four"])

    def test_coverage_hits_are_not_representative_number_hits(self):
        normal = self.result["normal"]
        special = int(self.result["special"]["number"])
        self.assertEqual(len(set(normal + [special])), 7)
        self.assertIn(special, self.analysis["four_numbers"])
        hit = next(n for n in self.analysis["four_numbers"] if n not in normal + [special])
        evaluation = evaluate_groups(self.result, {"sno": str(hit)})
        self.assertTrue(evaluation["four_zodiac_hit"])
        summary = summarize_groups([evaluation, {"error": "missing"}])["zodiac_groups"]
        self.assertEqual(summary["four_zodiac"]["total"], 1)
        self.assertEqual(summary["four_zodiac"]["hit_rate"], 100)
        self.assertEqual(summary["four_zodiac"]["random_baseline"], round(17 / 49 * 100, 2))

    def test_target_zodiac_and_baseline_change_at_new_year(self):
        next_mapping = {n: "羊马蛇龙兔虎牛鼠猪狗鸡猴"[(n - 1) % 12] for n in range(1, 50)}
        metrics = evaluate_groups(self.result, {"sno": "1", "sno_zodiac": "羊"}, next_mapping)
        self.assertFalse(metrics["six_zodiac_hit"])
        self.assertFalse(metrics["four_zodiac_hit"])
        self.assertEqual(metrics["six_zodiac_baseline"], 24 / 49)
        self.assertEqual(metrics["four_zodiac_baseline"], 16 / 49)


class ZodiacIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="mark-six-zodiac-test-")
        cls.old_cwd = os.getcwd()
        cls.env = patch.dict(os.environ, {
            "DATABASE_URL": f"sqlite:///{cls.directory.name}/test.db",
            "SECRET_KEY": "isolated-test-key", "ENABLE_SCHEDULER": "0",
            "ENABLE_STARTUP_BACKTEST_WARMUP": "0", "ENABLE_STARTUP_ML_WARMUP": "0",
            "ENABLE_STARTUP_ZODIAC_BACKFILL": "0", "NOTIFY_ASYNC": "0",
        })
        cls.env.start()
        os.chdir(cls.directory.name)
        # Fail on accidental outbound traffic, including during app startup.
        cls.network = patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network"))
        cls.network.start()
        cls.module = importlib.import_module("app")
        cls.app = cls.module.app
        cls.app.config["TESTING"] = True
        cls.history = example_history()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.module.db.session.remove()
            cls.module.db.engine.dispose()
        cls.network.stop()
        os.chdir(cls.old_cwd)
        cls.env.stop()
        cls.directory.cleanup()

    def setUp(self):
        from models import LotteryDraw
        with self.app.app_context():
            LotteryDraw.query.delete()
            self.module.db.session.commit()
        self.module._hk_history_backfill_retry_after = 0.0

    def store_draws(self, draws, region="hk"):
        from models import LotteryDraw
        for draw in draws:
            self.module.db.session.add(LotteryDraw(
                region=region, draw_id=draw["id"], draw_date=draw["date"],
                normal_numbers=",".join(draw["no"]), special_number=draw["sno"],
                special_zodiac=draw.get("sno_zodiac", ""), raw_zodiac=draw.get("raw_zodiac", "")))
        self.module.db.session.commit()

    def test_hk_backfills_twelve_draws_without_future_data_or_overwriting(self):
        from models import LotteryDraw
        module = self.module
        future = deepcopy(self.history[0])
        future.update(id="2026326", date="2026-11-22")
        with self.app.app_context(), patch.object(module, "_get_number_to_zodiac_map", return_value=MAPPING), \
             patch.object(module, "fetch_hk_results_from_hkjc", return_value=[future] + self.history) as fetch:
            self.store_draws(self.history[:12])
            self.store_draws([future])
            result = module.get_local_recommendations("zodiac_rebound", self.history[:12], "hk")
            self.assertNotIn("error", result)
            analysis = result["model_meta"]["zodiac_rebound"]
            self.assertEqual(analysis["source_period"], self.history[0]["id"])
            self.assertEqual(analysis["history_count"], 20)
            self.assertEqual(analysis["four_zodiacs"], ["兔", "狗", "马", "鸡"])
            fetch.assert_called_once_with(limit=60)
            self.assertEqual(LotteryDraw.query.filter_by(region="hk").count(), 21)
            existing = LotteryDraw.query.filter_by(draw_id=self.history[0]["id"], region="hk").one()
            self.assertEqual(existing.raw_zodiac, self.history[0]["raw_zodiac"])
            second = module.get_local_recommendations("zodiac_rebound", self.history[:12], "hk")
            self.assertNotIn("error", second)
            self.assertEqual(fetch.call_count, 1)

    def test_hk_can_use_earlier_lunar_year_but_not_macau_history(self):
        module = self.module
        history = deepcopy(self.history)
        for i, draw in enumerate(history):
            dt = datetime(2027, 2, 10) - timedelta(days=i)
            draw.update(date=dt.strftime("%Y-%m-%d"), id=f"{dt.year}{150 - i:03d}")
        with self.app.app_context(), patch.object(module, "_get_number_to_zodiac_map", return_value=MAPPING), \
             patch.object(module, "fetch_hk_results_from_hkjc", return_value=[]) as fetch:
            self.store_draws(history[4:])
            result = module.get_local_recommendations("zodiac_rebound", history[:4], "hk")
            self.assertNotIn("error", result)
            self.assertEqual(result["model_meta"]["zodiac_rebound"]["history_count"], 20)
            fetch.assert_not_called()
            from models import LotteryDraw
            LotteryDraw.query.delete()
            module.db.session.commit()
            self.store_draws(history, region="macau")
            result = module.get_local_recommendations("zodiac_rebound", history[:4], "hk")
            self.assertIn("当前仅 4 期", result["error"])

    def test_hk_short_history_failure_is_actionable_and_throttled(self):
        module = self.module
        with self.app.app_context(), patch.object(module, "fetch_hk_results_from_hkjc", return_value=[]) as fetch, \
             patch.object(module, "_get_number_to_zodiac_map", return_value=MAPPING):
            self.store_draws(self.history[:12])
            for _ in range(2):
                result = module.get_local_recommendations("zodiac_rebound", self.history[:12], "hk")
                self.assertIn("当前仅 12 期", result["error"])
                self.assertIn("香港开奖数据导入", result["error"])
            self.assertEqual(fetch.call_count, 1)
            # Sufficient existing data and Macau requests do not call HK at all.
            module.get_local_recommendations("zodiac_rebound", self.history, "hk")
            module.get_local_recommendations("zodiac_rebound", self.history[:12], "macau")
            self.assertEqual(fetch.call_count, 1)

    def test_backtest_short_history_never_backfills_from_database_or_network(self):
        module = self.module
        with self.app.app_context(), patch.object(module, "fetch_hk_results_from_hkjc") as fetch, \
             patch.object(module, "_get_number_to_zodiac_map", return_value=MAPPING):
            self.store_draws(self.history)
            with module._temporary_strict_backtest_strategy(), self.assertRaisesRegex(ValueError, "当前仅 12"):
                module.get_local_recommendations("zodiac_rebound", self.history[:12], "hk")
            with module._temporary_backtest_cutoff_period("2026326"):
                result = module.get_local_recommendations("zodiac_rebound", self.history[:12], "hk")
                self.assertIn("当前仅 12", result["error"])
            fetch.assert_not_called()

    def test_hk_daily_sync_keeps_official_history_when_legacy_fails(self):
        module = self.module
        with patch.object(module, "fetch_hk_results_from_hkjc", return_value=self.history) as fetch, \
             patch.object(module.requests, "get") as legacy:
            legacy.side_effect = RuntimeError("legacy offline")
            self.assertEqual(module.load_hk_data(), self.history)
            fetch.assert_called_once_with(limit=60)
            legacy.side_effect = None
            legacy.return_value.json.return_value = "bad response format"
            self.assertEqual(module.load_hk_data(), self.history)

    def test_official_history_skips_incomplete_duplicate_or_invalid_balls(self):
        module = self.module
        valid = {"year": "2026", "no": 123, "drawDate": "2026-11-19+08:00", "status": "Result",
                 "drawResult": {"drawnNo": [1, 2, 3, 4, 5, 6], "xDrawnNo": 7}}
        invalid = []
        for special in (None, 0, 50, 1):
            draw = deepcopy(valid)
            draw["drawResult"]["xDrawnNo"] = special
            invalid.append(draw)
        pending = deepcopy(valid)
        pending["status"] = "Defined"
        with patch.object(module, "_hkjc_graph_post", return_value={"lotteryDraws": [valid, pending] + invalid}) as graph:
            records = module.fetch_hk_results_from_hkjc()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["id"], "2026123")
            self.assertEqual(graph.call_args.args[1]["lastNDraw"], 60)

    def test_api_persistence_and_repeated_read(self):
        from models import User, PredictionRecord, SHARED_PREDICTION_USER_ID
        with self.app.app_context():
            user = User(username="zodiac-test", email="zodiac@example.test", password_hash="unused", is_active=True)
            self.module.db.session.add(user)
            self.module.db.session.commit()
            user_id = user.id
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["is_active"] = True
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        with patch.object(self.module, "_get_prediction_data", return_value=(self.history, 2026)), \
             patch.object(self.module, "_get_number_to_zodiac_map", return_value={str(n): z for n, z in MAPPING.items()}):
            first = client.get("/api/predict?region=macau&strategy=zodiac_rebound&year=2026")
            self.assertEqual(first.status_code, 200, first.get_json())
            result = first.get_json()
            self.assertNotIn("error", result)
            self.assertEqual(result["model_meta"]["zodiac_rebound"]["four_zodiacs"], ["兔", "狗", "马", "鸡"])
            second = client.get("/api/predict?region=macau&strategy=zodiac_rebound&year=2026").get_json()
            self.assertEqual(second["model_meta"], result["model_meta"])
            self.assertEqual(second["recommendation_text"], result["recommendation_text"])
            with self.app.app_context():
                records = PredictionRecord.query.filter_by(strategy="zodiac_rebound").all()
                self.assertEqual(len(records), 1)
                self.assertIn("four_zodiacs", records[0].prediction_metadata)
                self.assertIn(records[0].user_id, (user_id, SHARED_PREDICTION_USER_ID))
                self.assertIsNone(records[0].zodiac_groups["six_hit"])
                records[0].actual_special_number = "16"
                records[0].actual_special_zodiac = "兔"
                records[0].is_result_updated = True
                self.module.db.session.commit()
                self.assertTrue(records[0].zodiac_groups["six_hit"])
                self.assertTrue(records[0].zodiac_groups["four_hit"])
            mobile = client.get("/api/mobile/predictions?strategy=zodiac_rebound&include_details=0&include_summaries=0")
            self.assertEqual(mobile.status_code, 200, mobile.get_json())
            groups = mobile.get_json()["items"][0]["zodiac_groups"]
            self.assertEqual(groups["four_zodiacs"], ["兔", "狗", "马", "鸡"])
            self.assertTrue(groups["four_hit"])
        self.assertEqual(self.app.test_client().get("/api/predict?strategy=zodiac_rebound").status_code, 401)

    def test_dispatch_deduplication_and_snapshot(self):
        module = self.module
        with self.app.app_context(), patch.object(module, "_get_number_to_zodiac_map", return_value=MAPPING):
            for region in ("hk", "macau"):
                result = module.get_local_recommendations("zodiac_rebound", self.history, region, variation_key="ignored")
                self.assertNotIn("error", result)
                old_text = result["recommendation_text"]
                old_special = int(result["special"]["number"])
                adjusted = module._ensure_period_unique_special(
                    result, "zodiac_rebound", region, "2026326", user_id=1,
                    prediction_zodiac_year=2026, used_special_numbers={old_special})
                self.assertNotEqual(int(adjusted["special"]["number"]), old_special)
                self.assertEqual(adjusted["recommendation_text"], old_text)
                self.assertIn(int(adjusted["special"]["number"]), adjusted["model_meta"]["zodiac_rebound"]["four_numbers"])
            self.assertIn("error", module.get_local_recommendations("zodiac_rebound", [], "hk"))
            self.assertEqual(module._strategy_backtest_min_history("zodiac_rebound", "hk", 10), 20)
            target = deepcopy(self.history[0])
            target.update(id="2026326", date="2026-11-22")
            snapshot = module._build_backtest_snapshot_payload("macau", [target] + self.history, ["zodiac_rebound"])
            summary = snapshot["strategy_results"]["zodiac_rebound"]
            self.assertEqual(summary["total"], 1)
            self.assertEqual(summary["zodiac_groups"]["six_zodiac"]["total"], 1)
            cli = importlib.import_module("scripts.backtest_predictions")
            with patch.object(cli, "_load_region_draws", return_value=list(reversed(self.history)) + [target]):
                backtest = cli.run_backtest("macau", ["zodiac_rebound"], min_history=20)
            self.assertEqual(backtest["strategy_results"]["zodiac_rebound"]["zodiac_groups"], summary["zodiac_groups"])

    def test_record_group_rendering_and_invalid_metadata(self):
        from models import PredictionRecord
        record = PredictionRecord(strategy="zodiac_rebound", is_result_updated=True,
                                  actual_special_zodiac="蛇",
                                  prediction_metadata=self.module._serialize_prediction_metadata(
                                      build_prediction(self.history, MAPPING)["model_meta"]))
        with self.app.app_context():
            self.assertTrue(record.zodiac_groups["six_hit"])
            self.assertFalse(record.zodiac_groups["four_hit"])
            # Compile the real macro with a minimal parent, independent of page data.
            source, _, _ = self.app.jinja_env.loader.get_source(self.app.jinja_env, "user/predictions.html")
            macro = source[source.index("{% macro zodiac_group_summary"):source.index("{% macro render_prediction_card")]
            template = self.app.jinja_env.from_string(macro + "{{ zodiac_group_summary(prediction) }}")
            html = template.render(prediction=record)
            self.assertIn("综合六肖", html)
            self.assertIn("兔、狗、马、鸡", html)
            self.assertIn("✓ 命中", html)
            self.assertIn("未命中", html)
            for invalid in ("broken", '[]', '{"zodiac_rebound":null}'):
                record.prediction_metadata = invalid
                self.assertEqual(record.zodiac_groups, {})

    def test_zodiac_groups_reach_station_email_and_telegram(self):
        from models import User, PredictionRecord, UserNotification
        import notification_service
        result = build_prediction(self.history, MAPPING)
        prediction = PredictionRecord(
            strategy="zodiac_rebound", region="macau", period="2026326",
            normal_numbers=",".join(map(str, result["normal"])),
            special_number=result["special"]["number"], special_zodiac=result["special"]["sno_zodiac"],
            prediction_metadata=self.module._serialize_prediction_metadata(result["model_meta"]),
        )
        normal = SimpleNamespace(strategy="hot", normal_numbers="1,2,3,4,5,6", special_number="42", special_zodiac="牛")
        config = {"station_enabled": True, "email_enabled": True, "telegram_enabled": True,
                  "telegram_bot_token": "test-only-token", "telegram_chat_id": "-1001234567890"}
        with self.app.app_context(), \
             patch.object(notification_service, "get_user_notification_config", return_value=config), \
             patch.object(notification_service, "send_html_email") as email, \
             patch.object(notification_service.requests, "post") as post, \
             patch.object(self.module, "_get_number_to_zodiac_map", return_value=MAPPING):
            user = User(username="notification-test", email="notice@example.test", password_hash="unused", is_active=True)
            self.module.db.session.add(user)
            self.module.db.session.commit()
            for show_normal in (False, True):
                with self.subTest(show_normal=show_normal):
                    user.show_normal_numbers = show_normal
                    self.module.send_combined_prediction_email(user, [prediction, normal], "macau", "2026326")
                    telegram_text = post.call_args.kwargs["json"]["text"]
                    email_html = email.call_args.args[2]
                    notice = UserNotification.query.filter_by(user_id=user.id).order_by(UserNotification.id.desc()).first()
                    for content in (telegram_text, email_html, notice.content):
                        self.assertIn("综合六肖", content)
                        self.assertIn("兔、狗、牛、马、鸡、蛇", content)
                        self.assertIn("精选四肖", content)
                        self.assertIn("兔、狗、马、鸡", content)
                    self.assertIn("热门预测:", telegram_text)
                    self.assertEqual("平码 1,2,3,4,5,6" in telegram_text, show_normal)
                    self.assertNotIn("生肖遗漏回补: 特码", telegram_text)
                    self.assertEqual(notice.content_format, "html")

    def test_notice_uses_saved_snapshot_and_handles_invalid_group_metadata(self):
        from models import PredictionRecord
        module = self.module
        metadata = build_prediction(self.history, MAPPING)["model_meta"]
        prediction = PredictionRecord(strategy="zodiac_rebound", period="2026326", region="macau",
                                      special_number="4", special_zodiac="兔", normal_numbers="1,2,3,5,6,7",
                                      actual_special_number="4", actual_special_zodiac="兔",
                                      prediction_metadata=module._serialize_prediction_metadata(metadata))
        user = SimpleNamespace(username="saved-test", show_normal_numbers=False)
        with self.app.app_context(), patch.object(module, "notify_user") as notify, \
             patch.object(module, "get_local_recommendations", side_effect=AssertionError("must not recompute")):
            snapshot = module._winning_prediction_snapshot(prediction)
            self.assertEqual(snapshot["prediction_metadata"], prediction.prediction_metadata)
            module.send_combined_winning_email(user, [SimpleNamespace(**snapshot)], "macau")
            self.assertIn("综合六肖：兔、狗、牛、马、鸡、蛇", notify.call_args.args[2])
            self.assertIn("精选四肖：兔、狗、马、鸡", notify.call_args.args[2])
            for invalid in ("", "broken", '[]', '{"zodiac_rebound":null}',
                            '{"zodiac_rebound":{"six_zodiacs":"兔狗牛马鸡蛇","four_zodiacs":[]}}'):
                prediction.prediction_metadata = invalid
                module.send_combined_prediction_email(user, [prediction], "macau", "2026326")
                self.assertIn("未保存完整六肖、四肖", notify.call_args.args[2])
                self.assertNotIn("特码 4", notify.call_args.args[2])

    def test_templates_compile_and_strategy_is_selectable(self):
        with self.app.app_context():
            for name in ("control_panel.html", "user/predictions.html", "admin/predictions.html"):
                self.app.jinja_env.get_template(name)
            from api_mobile import STRATEGY_KEYS
            from user import AUTO_STRATEGY_META
            self.assertIn("zodiac_rebound", STRATEGY_KEYS)
            self.assertIn("zodiac_rebound", [s["key"] for s in AUTO_STRATEGY_META])
            self.assertIn("zodiac_rebound", self.module.AUTO_PREDICTION_STRATEGY_KEYS)
            self.assertNotIn("zodiac_rebound", self.module.AUTO_OPTIMIZE_STRATEGIES)


if __name__ == "__main__":
    unittest.main()
