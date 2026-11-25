"""Bounded strategy specification validator and deterministic interpreter."""

from collections import Counter

from strategy_platform.catalog import get_strategy_keys

SCHEMA_VERSION = 1
REGIONS = {"hk", "macau"}
ZODIACS = tuple("鼠牛虎兔龙蛇马羊猴鸡狗猪")
RANK_METRICS = {"frequency", "omission", "special_frequency"}
GROUP_FIELDS = {"id", "label", "size", "metric", "order"}


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def validate_strategy_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError("strategy spec must be an object")
    if set(spec) - {"schema_version", "kind", "regions", "window", "normal_count", "rank_metric", "rank_order", "groups", "source_key", "source_fields", "strategy_key", "parameters"}:
        raise ValueError("strategy spec contains unsupported fields")
    if type(spec.get("schema_version")) is not int or spec["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported strategy schema version")
    kind = spec.get("kind")
    if not isinstance(kind, str) or kind not in {"builtin_config", "number_selection", "zodiac_groups", "external_selection"}:
        raise ValueError("unsupported strategy output kind")
    regions = spec.get("regions", ["hk", "macau"])
    if not isinstance(regions, list) or not 1 <= len(regions) <= 2 or any(not isinstance(region, str) or region not in REGIONS for region in regions):
        raise ValueError("regions must contain hk and/or macau")

    if kind == "builtin_config":
        strategy_key = str(spec.get("strategy_key") or "").strip()
        if strategy_key not in get_strategy_keys(capability="number_tuning") + ["zodiac_rebound"]:
            raise ValueError("builtin_config requires a tunable built-in strategy")
        parameters = spec.get("parameters")
        if not isinstance(parameters, dict) or (not parameters and strategy_key != "zodiac_rebound"):
            raise ValueError("builtin_config requires strategy parameters")
        if strategy_key == "zodiac_rebound":
            if set(parameters) - {"window", "parity_streak_threshold"}:
                raise ValueError("生肖遗漏回补仅支持统计窗口和单双连开阈值")
            _integer(parameters.get("window", 20), "window", 10, 240)
            _integer(parameters.get("parity_streak_threshold", 5), "parity_streak_threshold", 2, 20)
            return spec
        if set(parameters) - {"window", "pool", "special_pool", "trend_window"}:
            raise ValueError("builtin_config contains unsupported parameters")
        if "window" in parameters:
            _integer(parameters["window"], "window", 1, 240)
        if "pool" in parameters:
            _integer(parameters["pool"], "pool", 8, 24)
        if "special_pool" in parameters:
            _integer(parameters["special_pool"], "special_pool", 6, 14)
        if "trend_window" in parameters:
            _integer(parameters["trend_window"], "trend_window", 1, 60)
        return spec
    if kind in {"number_selection", "zodiac_groups"}:
        _integer(spec.get("window", 20), "window", 1, 240)
    if kind == "number_selection":
        _integer(spec.get("normal_count", 6), "normal_count", 1, 20)
        if not isinstance(spec.get("rank_metric", "frequency"), str) or spec.get("rank_metric", "frequency") not in RANK_METRICS:
            raise ValueError("unsupported number ranking metric")
        if not isinstance(spec.get("rank_order", "highest"), str) or spec.get("rank_order", "highest") not in {"highest", "lowest"}:
            raise ValueError("rank_order must be highest or lowest")
    elif kind == "zodiac_groups":
        groups = spec.get("groups")
        if not isinstance(groups, list) or not groups or len(groups) > 4:
            raise ValueError("zodiac_groups requires one to four groups")
        seen_ids = set()
        for group in groups:
            if not isinstance(group, dict) or set(group) - GROUP_FIELDS:
                raise ValueError("zodiac group contains unsupported fields")
            group_id = str(group.get("id") or "").strip()
            if not group_id or len(group_id) > 40 or group_id in seen_ids:
                raise ValueError("zodiac group IDs must be unique and non-empty")
            if len(str(group.get("label") or group_id)) > 80:
                raise ValueError("zodiac group labels may not exceed 80 characters")
            seen_ids.add(group_id)
            _integer(group.get("size"), "group size", 1, 12)
            if not isinstance(group.get("metric", "omission"), str) or group.get("metric", "omission") not in RANK_METRICS:
                raise ValueError("unsupported zodiac ranking metric")
            if not isinstance(group.get("order", "highest"), str) or group.get("order", "highest") not in {"highest", "lowest"}:
                raise ValueError("group order must be highest or lowest")
    elif kind == "external_selection":
        source_key = str(spec.get("source_key") or "").strip()
        if source_key != "macau_five_zodiac":
            raise ValueError("external strategy requires a registered source key")
        fields = spec.get("source_fields")
        if not isinstance(fields, list) or not 1 <= len(fields) <= 2 or any(not isinstance(field, str) or field not in {"numbers", "zodiacs"} for field in fields):
            raise ValueError("source_fields may contain numbers and/or zodiacs")
    return spec


def _number(value):
    text = str(value or "").strip()
    if not text.isdigit() or not 1 <= int(text) <= 49:
        raise ValueError("draw history contains an invalid number")
    return int(text)


def _values(value):
    return value.split(",") if isinstance(value, str) else list(value or [])


def _ranked(items, metric, order):
    direction = -1 if order == "highest" else 1
    return sorted(items, key=lambda item: (direction * item[1], item[0]))


def _metric_counts(draws, window):
    history = list(draws or [])[:window]
    frequency = Counter()
    special_frequency = Counter()
    omission = {}
    for index, draw in enumerate(history):
        numbers = [_number(value) for value in _values(draw.get("no"))]
        special = _number(draw.get("sno"))
        for number in set(numbers + [special]):
            frequency[number] += 1
            omission.setdefault(number, index)
        special_frequency[special] += 1
    return history, frequency, special_frequency, omission


def _number_metric(metric, number, frequency, special_frequency, omission, window):
    if metric == "frequency":
        return frequency.get(number, 0)
    if metric == "special_frequency":
        return special_frequency.get(number, 0)
    return omission.get(number, window)


def execute_strategy_spec(spec, draws, region, target_period, strategy_key,
                          number_to_zodiac=None, source_snapshot=None):
    validate_strategy_spec(spec)
    if region not in spec.get("regions", ["hk", "macau"]):
        raise ValueError("strategy does not support the selected region")

    kind = spec["kind"]
    if kind == "builtin_config":
        raise ValueError("builtin_config must run through its registered strategy adapter")
    if kind == "external_selection":
        if not isinstance(source_snapshot, dict):
            raise ValueError("registered source snapshot is unavailable")
        payload = source_snapshot.get("payload") or {}
        groups = []
        for field, label in (("numbers", "外部号码"), ("zodiacs", "外部生肖")):
            if field in spec["source_fields"]:
                values = payload.get(field) or []
                if not values:
                    raise ValueError(f"source snapshot has no {field}")
                groups.append({"id": field, "label": label, "items": list(values)})
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "external_selection",
            "strategy_key": strategy_key,
            "period": str(target_period),
            "source": {
                "source_key": spec["source_key"],
                "source_period": str(source_snapshot.get("period") or ""),
                "published_at": source_snapshot.get("published_at"),
                "snapshot_id": source_snapshot.get("id"),
                "fetched_at": source_snapshot.get("fetched_at"),
                "parser_version": source_snapshot.get("parser_version"),
                "quality_status": source_snapshot.get("quality_status"),
                "sha256": source_snapshot.get("sha256"),
            },
            "groups": groups,
        }

    window = spec.get("window", 20)
    history, frequency, special_frequency, omission = _metric_counts(draws, window)
    if not history:
        raise ValueError("strategy requires completed draw history")
    if kind == "number_selection":
        metric = spec.get("rank_metric", "frequency")
        order = spec.get("rank_order", "highest")
        ranked = _ranked(
            ((number, _number_metric(metric, number, frequency, special_frequency, omission, window))
             for number in range(1, 50)),
            metric,
            order,
        )
        normal_count = spec.get("normal_count", 6)
        normal = [number for number, _ in ranked[:normal_count]]
        remaining = [(number, score) for number, score in ranked if number not in normal]
        special = remaining[0][0]
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "number_selection",
            "strategy_key": strategy_key,
            "period": str(target_period),
            "selection": {"normal": normal, "special": {"number": f"{special:02d}"}},
            "rationale": {"metric": metric, "window": min(window, len(history)), "rank_order": order},
            "provenance": {"input_cutoff_period": str(history[0].get("id") or "")},
        }

    zodiac_frequency = Counter()
    zodiac_special_frequency = Counter()
    zodiac_omission = {}
    for index, draw in enumerate(history):
        raw_mapping = number_to_zodiac(draw) if callable(number_to_zodiac) else number_to_zodiac
        mapping = {int(number): str(zodiac).strip() for number, zodiac in (raw_mapping or {}).items()}
        if set(mapping) != set(range(1, 50)):
            raise ValueError("zodiac strategy requires a complete number-to-zodiac mapping")
        normal_numbers = [_number(value) for value in _values(draw.get("no"))]
        special_number = _number(draw.get("sno"))
        raw = [_normalise_zodiac(value) for value in _values(draw.get("raw_zodiac"))]
        zodiacs = [
            (raw[pos] if pos < len(raw) else "") or mapping[number]
            for pos, number in enumerate(normal_numbers)
        ]
        special_zodiac = _normalise_zodiac(draw.get("sno_zodiac"))
        if not special_zodiac and len(raw) > 6:
            special_zodiac = raw[6]
        special_zodiac = special_zodiac or mapping[special_number]
        for zodiac in zodiacs + [special_zodiac]:
            zodiac_frequency[zodiac] += 1
            zodiac_omission.setdefault(zodiac, index)
        zodiac_special_frequency[special_zodiac] += 1

    output_groups = []
    for group in spec["groups"]:
        metric = group.get("metric", "omission")
        counts = {
            "frequency": zodiac_frequency,
            "omission": {z: zodiac_omission.get(z, window) for z in ZODIACS},
            "special_frequency": zodiac_special_frequency,
        }[metric]
        ranked_zodiacs = _ranked(
            ((zodiac, counts.get(zodiac, 0)) for zodiac in ZODIACS),
            metric,
            group.get("order", "highest"),
        )
        selected = [zodiac for zodiac, _ in ranked_zodiacs[:group["size"]]]
        output_groups.append({"id": group["id"], "label": group.get("label") or group["id"], "items": selected})

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "zodiac_groups",
        "strategy_key": strategy_key,
        "period": str(target_period),
        "groups": output_groups,
        "rationale": {"window": min(window, len(history))},
        "provenance": {"input_cutoff_period": str(history[0].get("id") or "")},
    }


def _normalise_zodiac(value):
    text = str(value or "").strip().translate(str.maketrans("龍馬雞豬", "龙马鸡猪"))
    return text if text in ZODIACS else ""
