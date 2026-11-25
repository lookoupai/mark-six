"""Versioned, JSON-safe result envelopes for strategy predictions."""

from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from strategy_platform.catalog import get_strategy


RESULT_SCHEMA_VERSION = 1


def build_strategy_result(strategy_key, legacy_result, target_period=None):
    """Wrap a legacy prediction without changing the legacy response fields."""
    strategy = get_strategy(strategy_key)
    if strategy is None:
        raise ValueError(f"unknown prediction strategy: {strategy_key}")

    source = legacy_result or {}
    if strategy.output_schema_id == "zodiac_groups":
        analysis = deepcopy((source.get("model_meta") or {}).get("zodiac_rebound") or {})
        six = list(analysis.get("six_zodiacs") or [])
        four = list(analysis.get("four_zodiacs") or [])
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "kind": "zodiac_groups",
            "availability": "complete" if len(six) == 6 and len(four) == 4 else "incomplete_legacy_data",
            "strategy_key": strategy.key,
            "period": str(target_period or ""),
            "groups": [
                {"id": "six", "label": "综合六肖", "items": six,
                 "numbers": list(analysis.get("six_numbers") or [])},
                {"id": "four", "label": "精选四肖", "items": four,
                 "numbers": list(analysis.get("four_numbers") or [])},
            ],
            "representative_selection": {
                "normal": list(source.get("normal") or []),
                "special": deepcopy(source.get("special") or {}),
            },
            "rationale": {
                "summary": str(source.get("recommendation_text") or ""),
                "statistics": deepcopy(analysis.get("statistics") or []),
            },
            "provenance": {
                "source_period": str(analysis.get("source_period") or ""),
                "history_count": int(analysis.get("history_count") or 0),
                "strategy_version": int(analysis.get("version") or 1),
            },
        }

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "kind": "number_selection",
        "strategy_key": strategy.key,
        "period": str(target_period or ""),
        "selection": {
            "normal": list(source.get("normal") or []),
            "special": deepcopy(source.get("special") or {}),
        },
        "rationale": {
            "summary": str(source.get("recommendation_text") or ""),
            "details": deepcopy(source.get("model_meta") or {}),
        },
    }


def attach_strategy_result(payload, strategy_key, legacy_result=None, target_period=None):
    """Attach the structured result while preserving existing API properties."""
    result = dict(payload or {})
    source = legacy_result if legacy_result is not None else result
    result["result"] = build_strategy_result(strategy_key, source, target_period)
    return result


def _normal_number(value):
    text = str(value or "").strip()
    if not text.isdigit() or not 1 <= int(text) <= 49:
        raise ValueError("result contains an invalid number")
    return str(int(text))


def _timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # Draw dates are local Hong Kong/Macau times in the existing database.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(timezone.utc)


def evaluate_result(prediction, draw, number_to_zodiac):
    """Evaluate a saved result identically in settlement and walk-forward tests.

    Baselines express coverage of 49 equally likely special balls. External
    selections without evidence of availability before the draw are displayed
    but contribute zero fair samples.
    """
    actual = _normal_number(draw.get("sno", draw.get("special")))
    mapping = {int(n): str(z).translate(str.maketrans("龍馬雞豬", "龙马鸡猪"))
               for n, z in (number_to_zodiac or {}).items()}
    actual_zodiac = str(draw.get("sno_zodiac", draw.get("special_zodiac")) or "").strip()
    actual_zodiac = actual_zodiac.translate(str.maketrans("龍馬雞豬", "龙马鸡猪")) or mapping.get(int(actual), "")
    outcome = {"period": str(draw.get("id") or prediction.get("period") or ""), "evaluated": True}
    kind = prediction.get("kind")
    if kind == "number_selection":
        selection = prediction.get("selection") or {}
        normal = {_normal_number(value) for value in selection.get("normal") or []}
        special = _normal_number((selection.get("special") or {}).get("number"))
        return {**outcome, "special_hit": special == actual,
                "normal_hit": actual in normal, "normal_coverage_hit": actual in normal,
                "special_random_baseline": 1 / 49, "normal_random_baseline": len(normal) / 49,
                "sample_count": 1}
    if kind not in {"zodiac_groups", "external_selection"}:
        raise ValueError("unknown result kind")
    eligible = True
    if kind == "external_selection":
        source = prediction.get("source") or {}
        try:
            eligible = (str(source.get("source_period")) == outcome["period"]
                        and bool(source.get("published_at")) and bool(draw.get("date"))
                        and _timestamp(source["published_at"]) < _timestamp(draw["date"]))
        except (TypeError, ValueError):
            eligible = False
        outcome["timing_verified"] = eligible
    outcome["groups"] = {}
    for group in prediction.get("groups") or []:
        items = group.get("items") or []
        if kind == "external_selection" and group.get("id") == "numbers":
            numbers = {_normal_number(value) for value in items}
            hit, baseline = actual in numbers, len(numbers) / 49
        else:
            if not actual_zodiac or len(mapping) != 49:
                raise ValueError("zodiac settlement needs a complete target-date mapping")
            zodiacs = {str(value).translate(str.maketrans("龍馬雞豬", "龙马鸡猪")) for value in items}
            hit = actual_zodiac in zodiacs
            baseline = sum(zodiac in zodiacs for zodiac in mapping.values()) / 49
        outcome["groups"][group["id"]] = {"hit": hit, "random_baseline": baseline,
                                            "sample_count": int(eligible)}
    return outcome
