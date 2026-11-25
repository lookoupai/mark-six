"""Compatibility migration for legacy automatic-prediction preferences."""

import json

from models import (
    StrategyAuditEvent,
    StrategyDefinition,
    StrategySubscription,
    StrategyWorkspaceMembership,
    SystemConfig,
    User,
    db,
)
from strategy_platform.access import get_default_workspace_for_owner
from strategy_platform.catalog import get_strategy_keys

LEGACY_SUBSCRIPTION_MIGRATION_KEY = "strategy_platform_legacy_subscription_migration_v1"


def _builtin_definitions():
    strategy_keys = get_strategy_keys(capability="scheduled_run")
    definitions = StrategyDefinition.query.filter(
        StrategyDefinition.workspace_id.is_(None),
        StrategyDefinition.slug.in_([f"builtin-{key}" for key in strategy_keys]),
    ).all()
    return {
        definition.slug.removeprefix("builtin-"): definition
        for definition in definitions
    }


def _legacy_strategy_keys(user):
    scheduled_keys = set(get_strategy_keys(capability="scheduled_run"))
    raw = str(getattr(user, "auto_prediction_strategies", "") or "")
    selected = [key for key in raw.split(",") if key in scheduled_keys] if raw else []
    if not selected:
        selected = get_strategy_keys(capability="number_tuning")
    return list(dict.fromkeys(selected))


def _legacy_regions(user):
    return str(getattr(user, "auto_prediction_regions", "") or "hk,macau")


def _sync_user_subscription_rows(user, workspace, definitions):
    selected_keys = set(_legacy_strategy_keys(user))
    regions = _legacy_regions(user)
    subscriptions = {
        subscription.strategy_id: subscription
        for subscription in StrategySubscription.query.filter_by(user_id=user.id).filter(
            StrategySubscription.strategy_id.in_([item.id for item in definitions.values()])
        ).all()
    } if definitions else {}

    for key, definition in definitions.items():
        subscription = subscriptions.get(definition.id)
        selected = key in selected_keys
        if subscription is None and not selected:
            continue
        if subscription is None:
            subscription = StrategySubscription(
                workspace_id=workspace.id,
                user_id=user.id,
                strategy_id=definition.id,
                revision_policy="follow_active",
                run_mode="scheduled",
                enabled=True,
                regions=regions,
            )
            db.session.add(subscription)
            continue

        subscription.workspace_id = workspace.id
        subscription.revision_policy = "follow_active"
        subscription.pinned_revision_id = None
        subscription.run_mode = "scheduled"
        subscription.enabled = selected
        subscription.regions = regions


def sync_legacy_user_subscriptions(user):
    """Mirror one member's legacy settings into built-in V2 subscription rows."""
    from models import StrategyWorkspace

    workspace = StrategyWorkspace.query.filter_by(slug="friends", status="active").first()
    if workspace is None:
        return False
    membership = StrategyWorkspaceMembership.query.filter_by(
        workspace_id=workspace.id,
        user_id=user.id,
        status="active",
    ).first()
    if membership is None:
        return False
    _sync_user_subscription_rows(user, workspace, _builtin_definitions())
    return True


def migrate_legacy_auto_prediction_subscriptions():
    """Mirror preferences only for members already authorized by an owner.

    A migration is never evidence of consent to join a workspace. Removed
    memberships and author roles remain untouched on every restart.
    """
    owner_user = User.query.filter(
        User.is_admin.is_(True), User.is_active.is_(True),
    ).order_by(User.id.asc()).first()
    if owner_user is None:
        return 0
    workspace = get_default_workspace_for_owner(owner_user)
    if workspace is None or workspace.status != "active":
        return 0
    definitions = _builtin_definitions()
    users = User.query.join(
        StrategyWorkspaceMembership,
        StrategyWorkspaceMembership.user_id == User.id,
    ).filter(
        StrategyWorkspaceMembership.workspace_id == workspace.id,
        StrategyWorkspaceMembership.status == "active",
    ).all()
    for user in users:
        _sync_user_subscription_rows(user, workspace, definitions)
    migration_state = SystemConfig.query.filter_by(key=LEGACY_SUBSCRIPTION_MIGRATION_KEY).first()
    if migration_state is None:
        migration_state = SystemConfig(key=LEGACY_SUBSCRIPTION_MIGRATION_KEY,
                                       description="已授权成员的旧自动预测订阅镜像迁移")
        db.session.add(migration_state)
    migration_state.value = "completed"
    db.session.commit()
    return len(users)


def get_legacy_builtin_subscriptions(user_id):
    """Return all migrated built-in rows, including disabled rows, by strategy ID."""
    definitions = _builtin_definitions()
    if not definitions:
        return {}
    subscriptions = StrategySubscription.query.join(
        StrategyDefinition,
        StrategySubscription.strategy_id == StrategyDefinition.id,
    ).filter(
        StrategySubscription.user_id == user_id,
        StrategySubscription.run_mode == "scheduled",
        StrategyDefinition.workspace_id.is_(None),
        StrategyDefinition.slug.in_([f"builtin-{key}" for key in definitions]),
    ).all()
    return {
        key: subscription
        for key, definition in definitions.items()
        for subscription in subscriptions
        if subscription.strategy_id == definition.id
    }


def legacy_subscription_settings_match(user, subscriptions):
    """Compare the legacy preference fields with their V2 subscription mirror."""
    expected_keys = set(_legacy_strategy_keys(user))
    enabled_keys = {
        key for key, subscription in subscriptions.items()
        if subscription.enabled
    }
    if expected_keys != enabled_keys:
        return False
    expected_regions = _legacy_regions(user)
    return all(subscription.regions == expected_regions for subscription in subscriptions.values())
