"""Persistence helpers for catalog-backed strategy definitions and runs."""

import json
from datetime import datetime
from sqlalchemy.exc import IntegrityError

from models import (
    StrategyDefinition,
    StrategyRevision,
    StrategyRun,
    db,
)
from strategy_platform.catalog import get_strategy_catalog, get_strategy
from strategy_platform.dsl import execute_strategy_spec, validate_strategy_spec
from strategy_platform.runners import ExecutionContext, strategy_runner_registry


ENGINE_VERSION = "strategy-platform-v1"


def seed_builtin_strategies():
    """Keep one database definition/revision for each installed code strategy."""
    for item in get_strategy_catalog():
        slug = f"builtin-{item['key']}"
        definition = StrategyDefinition.query.filter_by(workspace_id=None, slug=slug).first()
        if definition is None:
            definition = StrategyDefinition(
                workspace_id=None,
                owner_user_id=None,
                slug=slug,
                name=item["label"],
                kind=item["kind"],
                visibility="public",
                status="active",
            )
            db.session.add(definition)
            db.session.flush()

        revision = StrategyRevision.query.filter_by(
            strategy_id=definition.id,
            revision_no=1,
        ).first()
        if revision is None:
            revision = StrategyRevision(
                strategy_id=definition.id,
                revision_no=1,
                schema_version=1,
                spec_json=json.dumps(
                    {"kind": "builtin", "strategy_key": item["key"]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                source="builtin",
                change_summary="系统内置策略目录",
                engine_compatibility=ENGINE_VERSION,
            )
            db.session.add(revision)
            db.session.flush()
        definition.active_revision_id = revision.id
    db.session.commit()


def add_strategy_run(
    strategy_key,
    region,
    target_period,
    result,
    input_cutoff=None,
    subject_user_id=None,
    triggered_by_user_id=None,
    subscription=None,
):
    """Stage a run in the caller's transaction using the active catalog revision."""
    descriptor = get_strategy(strategy_key)
    if descriptor is None:
        raise ValueError(f"unknown prediction strategy: {strategy_key}")

    slug = f"builtin-{descriptor.key}"
    definition = StrategyDefinition.query.filter_by(workspace_id=None, slug=slug).first()
    if definition is None or not definition.active_revision_id:
        raise ValueError(f"strategy definition is not initialized: {strategy_key}")

    revision = StrategyRevision.query.get(definition.active_revision_id)
    if revision is None:
        raise ValueError(f"active strategy revision is missing: {strategy_key}")

    subject_key = f"user:{subject_user_id}" if subject_user_id is not None else "shared"
    idempotency_key = (
        f"builtin:{definition.id}:{revision.id}:{region}:{target_period}:{subject_key}"
    )
    existing = StrategyRun.query.filter_by(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing

    run = StrategyRun(
        workspace_id=subscription.workspace_id if subscription is not None else None,
        strategy_id=definition.id,
        revision_id=revision.id,
        subscription_id=subscription.id if subscription is not None else None,
        subject_user_id=subject_user_id,
        strategy_key=descriptor.key,
        region=region,
        target_period=str(target_period),
        input_cutoff=str(input_cutoff or ""),
        engine_version=ENGINE_VERSION,
        result_schema_id=str(result.get("kind") or descriptor.output_schema_id),
        result_schema_version=int(result.get("schema_version") or 1),
        result_json=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        status="succeeded",
        finished_at=datetime.now(),
        triggered_by_user_id=triggered_by_user_id,
        idempotency_key=idempotency_key,
    )
    db.session.add(run)
    return run


def get_strategy_run_result(strategy_key, region, target_period, subject_user_id=None):
    descriptor = get_strategy(strategy_key)
    if descriptor is None:
        return None
    definition = StrategyDefinition.query.filter_by(
        workspace_id=None,
        slug=f"builtin-{descriptor.key}",
    ).first()
    if definition is None:
        return None
    query = StrategyRun.query.filter_by(
        strategy_id=definition.id,
        region=region,
        target_period=str(target_period),
        subject_user_id=subject_user_id,
    )
    run = query.order_by(StrategyRun.created_at.desc(), StrategyRun.id.desc()).first()
    if run is None:
        return None
    return json.loads(run.result_json)


def add_custom_strategy_run(workspace_id, definition, revision, user_id,
                            region, target_period, input_cutoff, result, subscription_id=None, error_code=None):
    idempotency_key = (
        f"custom:{definition.id}:{revision.id}:{region}:{target_period}:user:{user_id}"
    )
    existing = StrategyRun.query.filter_by(idempotency_key=idempotency_key).first()
    if existing is not None and existing.status == "succeeded":
        return existing
    values = dict(
        workspace_id=workspace_id,
        strategy_id=definition.id,
        revision_id=revision.id,
        subscription_id=subscription_id,
        subject_user_id=user_id,
        strategy_key=definition.public_id,
        region=region,
        target_period=str(target_period),
        input_cutoff=str(input_cutoff or ""),
        engine_version=ENGINE_VERSION,
        result_schema_id=str(result.get("kind") or "strategy_result"),
        result_schema_version=int(result.get("schema_version") or 1),
        result_json=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        status="failed" if error_code else "succeeded",
        error_code=error_code,
        finished_at=datetime.now(),
        triggered_by_user_id=user_id,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        run = existing
        for key, value in values.items():
            setattr(run, key, value)
        db.session.flush()
    else:
        run = StrategyRun(**values)
        try:
            with db.session.begin_nested():
                db.session.add(run)
                db.session.flush()
        except IntegrityError:
            run = StrategyRun.query.filter_by(idempotency_key=idempotency_key).one()
    return run


def execute_workspace_spec(spec, draws, region, target_period, strategy_key,
                           number_to_zodiac=None, source_snapshot=None, backtest=False, target_date=None):
    from app import (_is_period_before, _period_sort_key, _get_number_to_zodiac_map,
                     _temporary_backtest_cutoff_period, _temporary_strict_backtest_strategy)
    from models import ZodiacSetting

    validate_strategy_spec(spec)
    if region not in spec.get("regions", ["hk", "macau"]) or not str(target_period or "").isdigit():
        raise ValueError("strategy region or target period is invalid")
    history = sorted((draw for draw in (draws or []) if _is_period_before(draw.get("id"), target_period)),
                     key=lambda draw: _period_sort_key(draw.get("id")), reverse=True)
    if number_to_zodiac is None:
        number_to_zodiac = lambda draw: _get_number_to_zodiac_map(
            ZodiacSetting.get_zodiac_year_for_date(draw.get("date")))
    context = ExecutionContext(
        strategy_key=strategy_key,
        data=history,
        region=region,
        target_period=str(target_period),
        spec=spec,
        number_to_zodiac=number_to_zodiac,
        source_snapshot=source_snapshot,
        backtest=backtest,
        target_date=target_date,
    )
    # The legacy ML adapter can supplement supplied history from the DB. Carry
    # the cutoff through that adapter, including its learned feedback queries.
    with _temporary_backtest_cutoff_period(target_period), _temporary_strict_backtest_strategy(True):
        result = strategy_runner_registry.execute_kind(str(spec.get("kind") or ""), context)
    result.setdefault("provenance", {}).update({
        "input_cutoff_period": str(history[0].get("id") or "") if history else "",
        "engine_version": ENGINE_VERSION,
    })
    return result


def _execute_rule_dsl(context):
    return execute_strategy_spec(
        context.spec,
        context.data,
        context.region,
        context.target_period,
        context.strategy_key,
        number_to_zodiac=context.number_to_zodiac,
        source_snapshot=context.source_snapshot,
    )


def _execute_builtin_config(context):
    from app import build_strategy_result, get_local_recommendations, _temporary_strategy_config_override

    spec = context.spec
    builtin_key = spec["strategy_key"]
    if builtin_key == "zodiac_rebound":
        from zodiac_rebound import build_prediction
        if not context.data:
            raise ValueError("生肖遗漏回补需要至少 20 期完整开奖记录")
        historical_map = context.number_to_zodiac
        target_draw = {"date": context.target_date} if context.target_date else context.data[0]
        target_map = historical_map(target_draw) if callable(historical_map) else historical_map
        legacy_result = build_prediction(context.data, target_map,
                                         historical_map if callable(historical_map) else None,
                                         **spec.get("parameters", {}))
    else:
        with _temporary_strategy_config_override(context.region, builtin_key, spec["parameters"]):
            legacy_result = get_local_recommendations(builtin_key, context.data, context.region)
    if legacy_result.get("error"):
        raise ValueError("strategy adapter could not produce a valid result")
    result = build_strategy_result(builtin_key, legacy_result, target_period=context.target_period)
    result["strategy_key"] = context.strategy_key
    return result


strategy_runner_registry.register_kind("rule_dsl", _execute_rule_dsl)
strategy_runner_registry.register_kind("external_selection", _execute_rule_dsl)
strategy_runner_registry.register_kind("number_selection", _execute_rule_dsl)
strategy_runner_registry.register_kind("zodiac_groups", _execute_rule_dsl)
strategy_runner_registry.register_kind("builtin_config", _execute_builtin_config)
