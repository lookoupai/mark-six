"""Workspace membership APIs for strategy authors and owners."""

import json
import re
import uuid
import base64
from datetime import datetime, timedelta
import os
import time
from urllib.parse import quote

from flask import Blueprint, jsonify, request, session
import requests
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from rate_limiter import rate_limited

from models import (
    StrategyAuditEvent,
    StrategyBackupDestination,
    StrategyBackupOutbox,
    BacktestRun,
    StrategyDefinition,
    StrategyChatMessage,
    StrategyChatSession,
    StrategyProposal,
    StrategyRevision,
    StrategyRun,
    StrategySubscription,
    StrategyWorkspace,
    StrategyWorkspaceMembership,
    User,
    db,
)
from strategy_platform.access import get_default_workspace_for_owner, get_workspace_membership
from strategy_platform.backup import verify_private_repository
from strategy_platform.bundles import build_bundle, validate_bundle, canonical_json
from strategy_platform.catalog import get_strategy, get_strategy_catalog
from strategy_platform.dsl import validate_strategy_spec
from strategy_platform.service import ENGINE_VERSION, add_custom_strategy_run, execute_workspace_spec
from strategy_platform.sources import load_strategy_source_snapshot
from strategy_platform.results import evaluate_result
from strategy_platform.assistant import AssistantResponseError, client_restriction, parse_assistant_content


strategy_api_bp = Blueprint("strategy_api", __name__, url_prefix="/api/v2")


@strategy_api_bp.before_request
def validate_strategy_request():
    if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
        if request.content_length and request.content_length > 150_000:
            return _error("策略请求过大", 413, "request_too_large")
        if request.data and not isinstance(request.get_json(silent=True), dict):
            return _error("请求体必须是 JSON 对象", 400, "invalid_json")


def _error(message, status=400, code="bad_request"):
    return jsonify({"success": False, "error": code, "message": message}), status


def _active_user():
    user_id = session.get("user_id")
    if not user_id:
        return None, _error("请先登录", 401, "auth_required")
    user = User.query.get(user_id)
    if not user:
        session.clear()
        return None, _error("登录状态已失效，请重新登录", 401, "auth_required")
    user.check_and_update_activation_status()
    if not user.is_active:
        return None, _error("账号未激活或已过期", 403, "activation_required")
    return user, None


def _membership(workspace_id, user_id):
    return get_workspace_membership(workspace_id, user_id)


def _require_role(workspace_id, user_id, roles):
    membership = _membership(workspace_id, user_id)
    if membership is None or membership.role not in roles:
        return None, _error("没有此工作区权限", 403, "workspace_forbidden")
    return membership, None


def _audit(workspace_id, actor_id, action, resource_type, resource_id, details=None):
    db.session.add(StrategyAuditEvent(
        workspace_id=workspace_id,
        actor_user_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        details_json=json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
    ))


@strategy_api_bp.route("/workspaces", methods=["GET"])
def list_workspaces():
    user, error = _active_user()
    if error:
        return error
    get_default_workspace_for_owner(user)
    memberships = StrategyWorkspaceMembership.query.filter_by(
        user_id=user.id,
        status="active",
    ).all()
    items = []
    for membership in memberships:
        workspace = StrategyWorkspace.query.get(membership.workspace_id)
        if workspace is None or workspace.status != "active":
            continue
        items.append({
            "id": workspace.id,
            "slug": workspace.slug,
            "name": workspace.name,
            "role": membership.role,
        })
    return jsonify({"success": True, "items": items})


@strategy_api_bp.route("/workspaces/<int:workspace_id>", methods=["PATCH"])
def rename_strategy_workspace(workspace_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    from strategy_platform.access import rename_workspace
    workspace = db.session.get(StrategyWorkspace, workspace_id)
    payload = request.get_json(silent=True) or {}
    try:
        changed = rename_workspace(workspace, user.id, payload.get("name"), payload.get("expected_name"))
    except ValueError as exc:
        return _error(str(exc))
    if not changed:
        db.session.rollback()
        return _error("工作区名称已更新，请刷新后重试", 409, "workspace_name_conflict")
    db.session.commit()
    return jsonify({"success": True, "workspace": {"id": workspace.id, "name": workspace.name, "slug": workspace.slug}})


def _strategy_name(payload, default):
    name = payload.get("name", default)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name):
        raise ValueError("策略名称须为 1 到 120 个可见字符")
    return name.strip()


@strategy_api_bp.route("/workspaces/<int:workspace_id>/members", methods=["GET"])
def list_workspace_members(workspace_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    memberships = StrategyWorkspaceMembership.query.filter_by(
        workspace_id=workspace_id,
    ).order_by(StrategyWorkspaceMembership.created_at.asc()).all()
    items = []
    for membership in memberships:
        member = User.query.get(membership.user_id)
        items.append({
            "user_id": membership.user_id,
            "username": member.username if member else "",
            "role": membership.role,
            "status": membership.status,
            "created_at": membership.created_at.isoformat() if membership.created_at else None,
        })
    return jsonify({"success": True, "items": items})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/members", methods=["POST"])
def add_workspace_member(workspace_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    role = str(payload.get("role") or "member").strip()
    if not username or role not in {"member", "strategy_author"}:
        return _error("请提供有效账号和 member/strategy_author 角色")
    member = User.query.filter_by(username=username).first()
    if member is None:
        return _error("该账号尚未注册", 404, "user_not_found")
    membership = StrategyWorkspaceMembership.query.filter_by(
        workspace_id=workspace_id,
        user_id=member.id,
    ).first()
    if membership is None:
        membership = StrategyWorkspaceMembership(
            workspace_id=workspace_id,
            user_id=member.id,
            role=role,
            status="active",
            invited_by_user_id=user.id,
        )
        db.session.add(membership)
    else:
        if membership.role == "owner":
            return _error("请使用工作区所有者转移接口", 409, "owner_transfer_required")
        membership.role = role
        membership.status = "active"
        membership.invited_by_user_id = user.id
    from strategy_platform.legacy import sync_legacy_user_subscriptions
    sync_legacy_user_subscriptions(member)
    _audit(workspace_id, user.id, "member.grant", "user", member.id, {"role": role})
    db.session.commit()
    return jsonify({
        "success": True,
        "member": {"user_id": member.id, "username": member.username, "role": role},
    }), 201


@strategy_api_bp.route("/workspaces/<int:workspace_id>/members/<int:member_user_id>", methods=["PATCH"])
def change_workspace_member_role(workspace_id, member_user_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    membership = StrategyWorkspaceMembership.query.filter_by(
        workspace_id=workspace_id,
        user_id=member_user_id,
        status="active",
    ).first()
    if membership is None:
        return _error("工作区成员不存在", 404, "member_not_found")
    if membership.role == "owner":
        return _error("请使用工作区所有者转移接口", 409, "owner_transfer_required")
    role = str((request.get_json(silent=True) or {}).get("role") or "").strip()
    if role not in {"member", "strategy_author"}:
        return _error("角色必须是 member 或 strategy_author")
    membership.role = role
    _audit(workspace_id, user.id, "member.role_changed", "user", member_user_id, {"role": role})
    db.session.commit()
    return jsonify({"success": True, "user_id": member_user_id, "role": role})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/members/<int:member_user_id>", methods=["DELETE"])
def remove_workspace_member(workspace_id, member_user_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    membership = StrategyWorkspaceMembership.query.filter_by(
        workspace_id=workspace_id,
        user_id=member_user_id,
        status="active",
    ).first()
    if membership is None:
        return _error("工作区成员不存在", 404, "member_not_found")
    if membership.role == "owner":
        return _error("请先转移工作区所有权", 409, "owner_transfer_required")
    membership.status = "removed"
    StrategySubscription.query.filter_by(
        workspace_id=workspace_id,
        user_id=member_user_id,
    ).update({StrategySubscription.enabled: False}, synchronize_session=False)
    _audit(workspace_id, user.id, "member.removed", "user", member_user_id)
    db.session.commit()
    return jsonify({"success": True})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/owner-transfer", methods=["POST"])
def transfer_workspace_owner(workspace_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        member_user_id = int(payload.get("user_id"))
    except (TypeError, ValueError):
        return _error("请提供有效的用户 ID")
    if member_user_id == user.id:
        return _error("新所有者必须是另一位工作区成员")
    current = _membership(workspace_id, user.id)
    target = _membership(workspace_id, member_user_id)
    if target is None:
        return _error("新所有者必须先加入工作区", 404, "member_not_found")
    if target.role == "owner":
        return _error("该成员已经是工作区所有者", 409, "owner_transfer_conflict")
    # A conditional update locks/claims the old owner row. Concurrent transfers
    # using the same stale owner cannot both promote different recipients.
    claimed = StrategyWorkspaceMembership.query.filter_by(
        id=current.id, role="owner", status="active",
    ).update({"role": "strategy_author"}, synchronize_session=False)
    if not claimed:
        db.session.rollback()
        return _error("工作区所有权已变化", 409, "owner_transfer_conflict")
    promoted = StrategyWorkspaceMembership.query.filter_by(
        id=target.id, status="active",
    ).filter(StrategyWorkspaceMembership.role != "owner").update(
        {"role": "owner"}, synchronize_session=False,
    )
    if not promoted:
        db.session.rollback()
        return _error("目标成员状态已变化", 409, "owner_transfer_conflict")
    _audit(workspace_id, user.id, "workspace.owner_transferred", "user", member_user_id)
    db.session.commit()
    return jsonify({"success": True, "owner_user_id": member_user_id})


def _strategy_payload(definition, revision=None):
    value = {
        "id": definition.public_id,
        "name": definition.name,
        "slug": definition.slug,
        "owner_user_id": definition.owner_user_id,
        "kind": definition.kind,
        "visibility": definition.visibility,
        "status": definition.status,
        "active_revision_id": definition.active_revision_id,
        "revision": {
            "id": revision.id,
            "revision_no": revision.revision_no,
            "schema_version": revision.schema_version,
            "spec": json.loads(revision.spec_json),
            "change_summary": revision.change_summary,
        } if revision else None,
    }

    user_id = session.get("user_id")
    membership = _membership(definition.workspace_id, user_id)
    subscription = StrategySubscription.query.filter_by(strategy_id=definition.id, user_id=user_id).first()
    value["subscription"] = {
        "enabled": subscription.enabled, "run_mode": subscription.run_mode,
        "regions": subscription.regions.split(","), "revision_policy": subscription.revision_policy,
        "pinned_revision_id": subscription.pinned_revision_id,
    } if subscription else None
    value["permissions"] = {
        "edit": bool(membership and _editable_strategy(definition, membership, user_id)),
        "archive": bool(membership and (membership.role == "owner" or _editable_strategy(definition, membership, user_id))),
        "clone": bool(membership and membership.role in {"owner", "strategy_author"} and definition.status == "active"),
        "run": bool(membership and definition.status == "active" and _visible_strategy(definition, membership, user_id)),
    }
    return value


def _visible_strategy(definition, membership, user_id):
    if membership.role == "owner":
        return True
    if definition.owner_user_id == user_id:
        return True
    return definition.visibility == "workspace" and definition.status == "active"


def _editable_strategy(definition, membership, user_id):
    return membership.role in {"owner", "strategy_author"} and definition.owner_user_id == user_id


def _enqueue_strategy_backup(workspace_id, definition, revision, user_id):
    try:
        payload_json = canonical_json(build_bundle(definition, revision))
        status, code = "pending", None
    except ValueError:
        # Local strategy publication remains independent of backup eligibility.
        payload_json, status, code = None, "failed", "backup_sensitive_content"
    destinations = StrategyBackupDestination.query.filter_by(
        workspace_id=workspace_id,
        enabled=True,
    ).all()
    for destination in destinations:
        outbox = StrategyBackupOutbox(
            workspace_id=workspace_id,
            strategy_id=definition.id,
            revision_id=revision.id,
            destination_id=destination.id,
            status=status, last_error_code=code,
            payload_json=payload_json,
        )
        db.session.add(outbox)


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies", methods=["GET"])
def list_workspace_strategies(workspace_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author", "member"})
    if error:
        return error
    definitions = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
    ).filter(StrategyDefinition.status.in_(("active", "draft"))).order_by(
        StrategyDefinition.created_at.desc()
    ).all()
    items = []
    audited_owner_view = False
    for definition in definitions:
        if not _visible_strategy(definition, membership, user.id):
            continue
        if membership.role == "owner" and definition.owner_user_id != user.id:
            _audit(workspace_id, user.id, "strategy.owner_viewed", "strategy", definition.public_id)
            audited_owner_view = True
        revision = StrategyRevision.query.get(definition.active_revision_id) if definition.active_revision_id else None
        if revision is None and definition.status == "draft":
            revision = StrategyRevision.query.filter_by(strategy_id=definition.id).order_by(
                StrategyRevision.revision_no.desc()
            ).first()
        items.append(_strategy_payload(definition, revision))
    if audited_owner_view:
        db.session.commit()
    return jsonify({
        "success": True,
        "builtins": get_strategy_catalog(),
        "items": items,
        "role": membership.role,
    })


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies", methods=["POST"])
def create_workspace_strategy(workspace_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    spec = payload.get("spec")
    if not name or len(name) > 120:
        return _error("策略名称长度必须为 1 到 120 个字符")
    try:
        validate_strategy_spec(spec)
    except ValueError as exc:
        return _error(str(exc), 422, "invalid_strategy_spec")

    public_id = str(uuid.uuid4())
    slug = str(payload.get("slug") or f"strategy-{public_id[:8]}").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,78}[a-z0-9]", slug):
        return _error("策略标识仅支持 3 到 80 位小写字母、数字和连字符")
    if StrategyDefinition.query.filter_by(workspace_id=workspace_id, slug=slug).first():
        return _error("该策略标识已使用", 409, "strategy_slug_conflict")

    definition = StrategyDefinition(
        public_id=public_id,
        workspace_id=workspace_id,
        owner_user_id=user.id,
        slug=slug,
        name=name,
        kind="builtin_config" if spec.get("kind") == "builtin_config" else "rule_dsl",
        visibility="private",
        status="active",
        created_by_user_id=user.id,
    )
    db.session.add(definition)
    db.session.flush()
    revision = StrategyRevision(
        strategy_id=definition.id,
        revision_no=1,
        schema_version=1,
        spec_json=json.dumps(spec, ensure_ascii=False, separators=(",", ":")),
        source="user",
        change_summary=str(payload.get("change_summary") or "创建策略")[:500],
        created_by_user_id=user.id,
    )
    db.session.add(revision)
    db.session.flush()
    definition.active_revision_id = revision.id
    _audit(workspace_id, user.id, "strategy.created", "strategy", definition.public_id)
    _enqueue_strategy_backup(workspace_id, definition, revision, user.id)
    db.session.commit()
    return jsonify({"success": True, "item": _strategy_payload(definition, revision)}), 201


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies/<string:public_id>", methods=["GET"])
def get_workspace_strategy(workspace_id, public_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author", "member"})
    if error:
        return error
    definition = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
        public_id=public_id,
    ).first_or_404()
    if not _visible_strategy(definition, membership, user.id):
        return _error("没有此策略权限", 403, "strategy_forbidden")
    revision = StrategyRevision.query.get(definition.active_revision_id) if definition.active_revision_id else None
    if revision is None and definition.status == "draft":
        revision = StrategyRevision.query.filter_by(strategy_id=definition.id).order_by(
            StrategyRevision.revision_no.desc()
        ).first()
    if membership.role == "owner" and definition.owner_user_id != user.id:
        _audit(workspace_id, user.id, "strategy.owner_viewed", "strategy", public_id)
        db.session.commit()
    return jsonify({"success": True, "item": _strategy_payload(definition, revision)})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies/<string:public_id>", methods=["PATCH"])
def revise_workspace_strategy(workspace_id, public_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    definition = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
        public_id=public_id,
    ).filter(StrategyDefinition.status.in_(("active", "draft"))).with_for_update().first_or_404()
    if not _editable_strategy(definition, membership, user.id):
        return _error("只能修改自己创建的策略", 403, "strategy_forbidden")
    payload = request.get_json(silent=True) or {}
    current_revision = StrategyRevision.query.filter_by(strategy_id=definition.id).order_by(StrategyRevision.revision_no.desc()).first()
    expected_revision = definition.active_revision_id or (current_revision.id if current_revision else None)
    if payload.get("expected_revision_id") != expected_revision:
        return _error("策略版本已更新，请重新载入后再保存", 409, "revision_conflict")
    spec = payload.get("spec", json.loads(current_revision.spec_json) if current_revision else None)
    try:
        name = _strategy_name(payload, definition.name)
    except ValueError as exc:
        return _error(str(exc))
    try:
        validate_strategy_spec(spec)
    except ValueError as exc:
        return _error(str(exc), 422, "invalid_strategy_spec")
    revision_no = current_revision.revision_no + 1 if current_revision else 1
    revision = StrategyRevision(
        strategy_id=definition.id,
        revision_no=revision_no,
        parent_revision_id=expected_revision,
        schema_version=1,
        spec_json=json.dumps(spec, ensure_ascii=False, separators=(",", ":")),
        source=str(payload.get("source") or "user")[:30],
        change_summary=str(payload.get("change_summary") or "更新策略")[:500],
        created_by_user_id=user.id,
    )
    db.session.add(revision)
    db.session.flush()
    previous_name = definition.name
    definition.name = name
    definition.kind = "builtin_config" if spec["kind"] == "builtin_config" else "rule_dsl"
    if definition.status == "active":
        definition.active_revision_id = revision.id
    _audit(workspace_id, user.id, "strategy.revised", "strategy", definition.public_id,
           {"revision_id": revision.id, "revision_no": revision_no, "previous_name": previous_name, "name": name})
    if definition.status == "active":
        _enqueue_strategy_backup(workspace_id, definition, revision, user.id)
    db.session.commit()
    return jsonify({"success": True, "item": _strategy_payload(definition, revision)})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies/<string:public_id>/share", methods=["POST"])
def share_workspace_strategy(workspace_id, public_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    definition = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
        public_id=public_id,
        status="active",
    ).first_or_404()
    if not _editable_strategy(definition, membership, user.id):
        return _error("只能分享自己创建的策略", 403, "strategy_forbidden")
    visibility = str((request.get_json(silent=True) or {}).get("visibility") or "").strip()
    if visibility not in {"private", "workspace"}:
        return _error("visibility 必须是 private 或 workspace")
    definition.visibility = visibility
    if visibility == "private":
        StrategySubscription.query.filter(
            StrategySubscription.strategy_id == definition.id,
            StrategySubscription.user_id != definition.owner_user_id,
        ).update({"enabled": False}, synchronize_session=False)
    _audit(workspace_id, user.id, "strategy.visibility_changed", "strategy", public_id,
           {"visibility": visibility})
    db.session.commit()
    return jsonify({"success": True, "visibility": visibility})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies/<string:public_id>/archive", methods=["POST"])
def archive_workspace_strategy(workspace_id, public_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    definition = StrategyDefinition.query.filter_by(workspace_id=workspace_id, public_id=public_id).with_for_update().first_or_404()
    if membership.role != "owner" and not _editable_strategy(definition, membership, user.id):
        return _error("没有此策略管理权限", 403, "strategy_forbidden")
    reason = (request.get_json(silent=True) or {}).get("reason")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        return _error("请填写 1 到 500 字的归档原因")
    definition.status = "archived"
    if definition.active_revision_id:
        for destination in StrategyBackupDestination.query.filter_by(workspace_id=workspace_id, enabled=True).all():
            if not StrategyBackupOutbox.query.filter_by(destination_id=destination.id,
                    revision_id=definition.active_revision_id, event_type="tombstone").first():
                db.session.add(StrategyBackupOutbox(workspace_id=workspace_id, strategy_id=definition.id,
                    revision_id=definition.active_revision_id, destination_id=destination.id, event_type="tombstone",
                    payload_json=canonical_json({"format": "mark-six-strategy-tombstone", "schema_version": 1,
                        "strategy_id": definition.public_id, "status": "archived"})))
    StrategySubscription.query.filter_by(strategy_id=definition.id).update({"enabled": False}, synchronize_session=False)
    _audit(workspace_id, user.id, "strategy.archived", "strategy", public_id, {"reason": reason.strip()})
    db.session.commit()
    return jsonify({"success": True, "status": "archived"})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies/<string:public_id>/clone", methods=["POST"])
def clone_workspace_strategy(workspace_id, public_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    source = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
        public_id=public_id,
        status="active",
    ).first_or_404()
    if not _visible_strategy(source, membership, user.id):
        return _error("没有此策略权限", 403, "strategy_forbidden")
    source_revision = StrategyRevision.query.get(source.active_revision_id)
    if source_revision is None:
        return _error("策略没有可复制的有效版本", 409, "revision_unavailable")
    payload = request.get_json(silent=True) or {}
    try:
        name = _strategy_name(payload, f"{source.name} 副本")
    except ValueError as exc:
        return _error(str(exc))
    new_public_id = str(uuid.uuid4())
    slug = str(payload.get("slug") or f"strategy-{new_public_id[:8]}").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,78}[a-z0-9]", slug):
        return _error("策略标识仅支持 3 到 80 位小写字母、数字和连字符")
    if StrategyDefinition.query.filter_by(workspace_id=workspace_id, slug=slug).first():
        return _error("该策略标识已使用", 409, "strategy_slug_conflict")
    definition = StrategyDefinition(
        public_id=new_public_id,
        workspace_id=workspace_id,
        owner_user_id=user.id,
        slug=slug,
        name=name,
        kind=source.kind,
        visibility="private",
        status="active",
        created_by_user_id=user.id,
    )
    db.session.add(definition)
    db.session.flush()
    revision = StrategyRevision(
        strategy_id=definition.id,
        revision_no=1,
        schema_version=source_revision.schema_version,
        spec_json=source_revision.spec_json,
        source="clone",
        change_summary=f"复制自 {source.name}"[:500],
        engine_compatibility=source_revision.engine_compatibility,
        created_by_user_id=user.id,
    )
    db.session.add(revision)
    db.session.flush()
    definition.active_revision_id = revision.id
    _audit(workspace_id, user.id, "strategy.cloned", "strategy", definition.public_id,
           {"source_strategy_id": source.public_id})
    _enqueue_strategy_backup(workspace_id, definition, revision, user.id)
    db.session.commit()
    return jsonify({"success": True, "item": _strategy_payload(definition, revision)}), 201


@strategy_api_bp.route("/workspaces/<int:workspace_id>/builtin-strategies/<string:strategy_key>/clone", methods=["POST"])
def clone_builtin_strategy(workspace_id, strategy_key):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    descriptor = get_strategy(strategy_key)
    if descriptor is None or not descriptor.enabled or strategy_key == "ai":
        return _error("该内置策略不能复制为可编辑策略", 422, "strategy_not_cloneable")
    payload = request.get_json(silent=True) or {}
    public_id = str(uuid.uuid4())
    try:
        name = _strategy_name(payload, f"{descriptor.label} 副本")
    except ValueError as exc:
        return _error(str(exc))
    slug = str(payload.get("slug") or f"strategy-{public_id[:8]}").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,78}[a-z0-9]", slug):
        return _error("策略标识仅支持 3 到 80 位小写字母、数字和连字符")
    if StrategyDefinition.query.filter_by(workspace_id=workspace_id, slug=slug).first():
        return _error("该策略标识已使用", 409, "strategy_slug_conflict")

    spec = {
        "schema_version": 1,
        "kind": "builtin_config",
        "regions": list(descriptor.regions),
        "strategy_key": strategy_key,
        "parameters": {} if strategy_key == "zodiac_rebound" else {"window": 40, "pool": 16, "special_pool": 8},
    }
    kind = "builtin_config"
    validate_strategy_spec(spec)

    definition = StrategyDefinition(
        public_id=public_id,
        workspace_id=workspace_id,
        owner_user_id=user.id,
        slug=slug,
        name=name,
        kind=kind,
        visibility="private",
        status="active",
        created_by_user_id=user.id,
    )
    db.session.add(definition)
    db.session.flush()
    revision = StrategyRevision(
        strategy_id=definition.id,
        revision_no=1,
        schema_version=1,
        spec_json=json.dumps(spec, ensure_ascii=False, separators=(",", ":")),
        source="builtin_clone",
        change_summary=f"复制内置策略：{descriptor.label}",
        created_by_user_id=user.id,
    )
    db.session.add(revision)
    db.session.flush()
    definition.active_revision_id = revision.id
    _audit(workspace_id, user.id, "strategy.builtin_cloned", "strategy", definition.public_id,
           {"builtin_strategy_key": strategy_key})
    _enqueue_strategy_backup(workspace_id, definition, revision, user.id)
    db.session.commit()
    return jsonify({"success": True, "item": _strategy_payload(definition, revision)}), 201


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies/<string:public_id>/runs", methods=["POST"])
def run_workspace_strategy(workspace_id, public_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author", "member"})
    if error:
        return error
    definition = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
        public_id=public_id,
        status="active",
    ).first_or_404()
    if not _visible_strategy(definition, membership, user.id):
        return _error("没有此策略运行权限", 403, "strategy_forbidden")
    if rate_limited(f"strategy-run:user:{user.id}", 30, 300):
        return _error("策略运行请求过于频繁", 429, "rate_limited")
    revision = StrategyRevision.query.get(definition.active_revision_id)
    if revision is None:
        return _error("策略没有可运行的有效版本", 409, "revision_unavailable")
    payload = request.get_json(silent=True) or {}
    region = str(payload.get("region") or "").strip()
    if region not in {"hk", "macau"}:
        return _error("region 必须是 hk 或 macau")

    from app import _get_next_period, _get_number_to_zodiac_map, _get_prediction_data, _infer_draw_year
    from models import ZodiacSetting

    draws, _ = _get_prediction_data(region, str(datetime.now().year))
    if not draws:
        return _error("无法加载开奖历史", 404, "draw_history_unavailable")
    latest = draws[0]
    target_period = _get_next_period(region, latest.get("id", ""), latest.get("date"))
    spec = json.loads(revision.spec_json)
    source_snapshot = None
    if spec.get("kind") == "external_selection":
        try:
            source_snapshot = load_strategy_source_snapshot(
                workspace_id,
                spec.get("source_key"),
                region,
                target_period,
                user.id,
            )
        except ValueError as exc:
            db.session.rollback()
            return _error(str(exc), 404, "source_data_unavailable")
    try:
        result = execute_workspace_spec(
            spec,
            draws,
            region,
            target_period,
            definition.public_id,
            source_snapshot=source_snapshot,
        )
    except ValueError as exc:
        db.session.rollback()
        run = add_custom_strategy_run(workspace_id, definition, revision, user.id, region,
            target_period, latest.get("id"), {}, error_code="strategy_execution_failed")
        _audit(workspace_id, user.id, "strategy.run_failed", "strategy_run", run.id,
               {"strategy_id": public_id, "region": region, "period": target_period,
                "error_code": "strategy_execution_failed"})
        db.session.commit()
        return _error("策略无法完成运行，请检查规则与历史数据", 422, "strategy_execution_failed")

    run = add_custom_strategy_run(
        workspace_id,
        definition,
        revision,
        user.id,
        region,
        target_period,
        latest.get("id"),
        result,
    )
    _audit(workspace_id, user.id, "strategy.run", "strategy_run", run.id,
           {"strategy_id": public_id, "region": region, "period": target_period})
    db.session.commit()
    return jsonify({
        "success": True,
        "run_id": run.id,
        "strategy_revision_id": revision.id,
        "result": json.loads(run.result_json),
    })


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies/<string:public_id>/runs", methods=["GET"])
def list_workspace_strategy_runs(workspace_id, public_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author", "member"})
    if error:
        return error
    definition = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
        public_id=public_id,
    ).first_or_404()
    if not _visible_strategy(definition, membership, user.id):
        return _error("没有此策略运行记录权限", 403, "strategy_forbidden")
    query = StrategyRun.query.filter_by(
        workspace_id=workspace_id,
        strategy_id=definition.id,
    )
    if membership.role != "owner":
        query = query.filter_by(subject_user_id=user.id)
    runs = query.order_by(StrategyRun.created_at.desc(), StrategyRun.id.desc()).limit(100).all()
    if membership.role == "owner":
        _audit(workspace_id, user.id, "strategy.runs_reviewed", "strategy", public_id)
        db.session.commit()
    return jsonify({
        "success": True,
        "items": [{
            "id": run.id,
            "revision_id": run.revision_id,
            "region": run.region,
            "period": run.target_period,
            "status": run.status,
            "result": json.loads(run.result_json),
            "settlement": json.loads(run.settlement_json) if run.settlement_json else None,
            "created_at": run.created_at.isoformat() if run.created_at else None,
        } for run in runs],
    })


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies/<string:public_id>/subscriptions", methods=["POST"])
def subscribe_workspace_strategy(workspace_id, public_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author", "member"})
    if error:
        return error
    definition = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
        public_id=public_id,
        status="active",
    ).first_or_404()
    if not _visible_strategy(definition, membership, user.id):
        return _error("没有此策略订阅权限", 403, "strategy_forbidden")
    payload = request.get_json(silent=True) or {}
    run_mode = str(payload.get("run_mode") or "manual").strip()
    if run_mode not in {"manual", "scheduled"}:
        return _error("run_mode 必须是 manual 或 scheduled")
    revision = StrategyRevision.query.get(definition.active_revision_id)
    spec = json.loads(revision.spec_json) if revision else {}
    regions = payload.get("regions") or spec.get("regions") or ["hk", "macau"]
    if not isinstance(regions, list) or not regions or any(region not in spec.get("regions", ["hk", "macau"]) for region in regions):
        return _error("订阅地区必须在策略支持范围内")
    policy = payload.get("revision_policy", "follow_active")
    pinned_id = payload.get("pinned_revision_id")
    if not isinstance(policy, str) or policy not in {"follow_active", "pinned"}:
        return _error("订阅版本策略无效")
    if policy == "pinned" and not StrategyRevision.query.filter_by(id=pinned_id, strategy_id=definition.id).first():
        return _error("固定修订不属于此策略")
    subscription = StrategySubscription.query.filter_by(
        user_id=user.id, strategy_id=definition.id,
    ).first()
    if subscription is None:
        subscription = StrategySubscription(
            workspace_id=workspace_id,
            user_id=user.id,
            strategy_id=definition.id,
            revision_policy="follow_active",
            regions=",".join(regions),
            run_mode=run_mode,
            enabled=True,
        )
        db.session.add(subscription)
    else:
        subscription.enabled = True
        subscription.run_mode = run_mode
        subscription.regions = ",".join(regions)
    subscription.revision_policy = policy
    subscription.pinned_revision_id = pinned_id if policy == "pinned" else None
    _audit(workspace_id, user.id, "strategy.subscribed", "strategy", public_id)
    db.session.commit()
    return jsonify({"success": True, "subscription_id": subscription.id}), 201


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies/<string:public_id>/subscriptions", methods=["DELETE"])
def unsubscribe_workspace_strategy(workspace_id, public_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author", "member"})
    if error:
        return error
    definition = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
        public_id=public_id,
    ).first_or_404()
    if not _visible_strategy(definition, membership, user.id):
        return _error("没有此策略订阅权限", 403, "strategy_forbidden")
    subscription = StrategySubscription.query.filter_by(
        user_id=user.id,
        strategy_id=definition.id,
    ).first()
    if subscription:
        subscription.enabled = False
        _audit(workspace_id, user.id, "strategy.unsubscribed", "strategy", public_id)
        db.session.commit()
    return jsonify({"success": True})


def _session_strategy_access(chat_session, user, membership, edit=False):
    if chat_session.user_id != user.id:
        return False
    if not chat_session.strategy_id:
        return membership.role in {"owner", "strategy_author"}
    definition = StrategyDefinition.query.get(chat_session.strategy_id)
    if definition is None or not _visible_strategy(definition, membership, user.id):
        return False
    if edit:
        return _editable_strategy(definition, membership, user.id)
    return True


def _chat_strategy_payload(definition):
    if definition is None:
        return None
    revision = db.session.get(StrategyRevision, definition.active_revision_id) if definition.active_revision_id else None
    return {"id": definition.public_id, "name": definition.name, "status": definition.status,
            "revision_id": revision.id if revision else None,
            "revision_no": revision.revision_no if revision else None,
            "spec": json.loads(revision.spec_json) if revision else None}


def _chat_message_payload(item):
    value = {"id": item.id, "role": item.role, "content": item.content,
             "created_at": item.created_at.isoformat() if item.created_at else None}
    # Earlier versions stored provider rejection messages as assistant text.
    # Preserve that history while clearly labelling it as a service response.
    if item.role == "assistant" and client_restriction(item.content):
        value["role"] = "service"
    return value


def _last_retryable_message(chat_session):
    latest_user = StrategyChatMessage.query.filter_by(session_id=chat_session.id, role="user").order_by(StrategyChatMessage.id.desc()).first()
    if latest_user:
        reply = StrategyChatMessage.query.filter(
            StrategyChatMessage.session_id == chat_session.id, StrategyChatMessage.role == "assistant",
            StrategyChatMessage.id > latest_user.id,
        ).order_by(StrategyChatMessage.id.desc()).first()
        if reply is None or client_restriction(reply.content):
            return latest_user
    return None


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategy-assistant/sessions", methods=["GET"])
def list_strategy_chat_sessions(workspace_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    limit = min(50, max(1, request.args.get("limit", 30, type=int)))
    page = min(1000, max(1, request.args.get("page", 1, type=int)))
    query = StrategyChatSession.query.outerjoin(StrategyDefinition, StrategyChatSession.strategy_id == StrategyDefinition.id).filter(
        StrategyChatSession.workspace_id == workspace_id, StrategyChatSession.user_id == user.id,
        db.or_(StrategyChatSession.strategy_id.is_(None), db.and_(
            StrategyDefinition.workspace_id == workspace_id, StrategyDefinition.owner_user_id == user.id)))
    search = str(request.args.get("q") or "").strip()[:120]
    if search:
        query = query.filter(db.or_(StrategyChatSession.title.contains(search, autoescape=True),
                                    StrategyDefinition.name.contains(search, autoescape=True)))
    strategy_id = request.args.get("strategy_id")
    if strategy_id:
        query = query.filter(StrategyDefinition.public_id == strategy_id)
    rows = query.order_by(StrategyChatSession.updated_at.desc(), StrategyChatSession.id.desc()).offset((page - 1) * limit).limit(limit + 1).all()
    items = []
    for row in rows[:limit]:
        definition = db.session.get(StrategyDefinition, row.strategy_id) if row.strategy_id else None
        items.append({"id": row.id, "title": row.title, "strategy_id": definition.public_id if definition else None,
                      "strategy_name": definition.name if definition else None, "status": row.status,
                      "updated_at": row.updated_at.isoformat()})
    return jsonify({"success": True, "items": items, "has_more": len(rows) > limit, "page": page})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategy-assistant/sessions", methods=["POST"])
def create_strategy_chat_session(workspace_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    definition = None
    public_id = str(payload.get("strategy_id") or "").strip()
    if "expected_strategy_id" in payload and payload["expected_strategy_id"] != (public_id or None):
        return _error("对话目标与当前策略不一致，请重新打开策略助手", 409, "chat_strategy_mismatch")
    if public_id:
        definition = StrategyDefinition.query.filter_by(workspace_id=workspace_id, public_id=public_id, status="active").first()
        if definition is None or not _editable_strategy(definition, membership, user.id):
            return _error("只能为自己创建的策略开启编辑会话", 403, "strategy_forbidden")
    title = str(payload.get("title") or (f"修改 {definition.name}" if definition else "新策略讨论"))[:160]
    chat_session = StrategyChatSession(workspace_id=workspace_id, user_id=user.id,
                                      strategy_id=definition.id if definition else None, title=title)
    db.session.add(chat_session)
    db.session.commit()
    return jsonify({"success": True, "session_id": chat_session.id, "strategy": _chat_strategy_payload(definition)}), 201


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategy-assistant/sessions/<int:session_id>/strategy", methods=["PATCH"])
def attach_strategy_chat_session(workspace_id, session_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    chat = StrategyChatSession.query.filter_by(id=session_id, workspace_id=workspace_id).first_or_404()
    if chat.user_id != user.id:
        return _error("只能关联自己的对话", 403, "chat_forbidden")
    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") is not True:
        return _error("请确认将此对话关联到所选策略", 400, "confirmation_required")
    definition = StrategyDefinition.query.filter_by(workspace_id=workspace_id,
        public_id=payload.get("strategy_id"), owner_user_id=user.id, status="active").first()
    if definition is None:
        return _error("只能关联自己的有效策略", 403, "strategy_forbidden")
    if chat.strategy_id is not None:
        return _error("此对话已关联策略，请新建对应策略的对话", 409, "chat_already_bound")
    changed = StrategyChatSession.query.filter_by(id=session_id, workspace_id=workspace_id,
        user_id=user.id, strategy_id=None, status="active").update(
            {"strategy_id": definition.id, "updated_at": datetime.now()}, synchronize_session=False)
    if not changed:
        db.session.rollback()
        return _error("对话状态已变化，请刷新", 409, "chat_changed")
    StrategyProposal.query.filter(StrategyProposal.session_id == session_id,
        StrategyProposal.status.in_(("validated", "backtested"))).update({"status":"superseded"}, synchronize_session=False)
    db.session.add(StrategyChatMessage(session_id=session_id, role="service",
        content=f"此对话已关联“{definition.name}”。之前未关联的讨论仅供参考，后续以该策略当前规则为准。"))
    _audit(workspace_id, user.id, "strategy.chat_attached", "strategy_chat_session", session_id,
           {"strategy_id": definition.public_id})
    db.session.commit()
    return jsonify({"success":True, "strategy":_chat_strategy_payload(definition)})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategy-assistant/sessions/<int:session_id>", methods=["DELETE"])
def delete_strategy_chat_session(workspace_id, session_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    chat = StrategyChatSession.query.filter_by(id=session_id, workspace_id=workspace_id).first_or_404()
    if chat.user_id != user.id:
        return _error("只能删除自己的对话", 403, "chat_forbidden")
    if (request.get_json(silent=True) or {}).get("confirm") is not True:
        return _error("请确认删除此对话及其草案和相关回测，已发布策略与运行记录会保留", 400, "confirmation_required")
    # Serialize against replies/attachment writes. Published strategies and runs
    # do not depend on a chat session and are deliberately preserved.
    claimed = StrategyChatSession.query.filter_by(id=session_id, user_id=user.id).update(
        {"status":"deleting"}, synchronize_session=False)
    if not claimed:
        db.session.rollback()
        return _error("对话已删除", 409, "chat_changed")
    backtest_ids = [row[0] for row in db.session.query(StrategyProposal.backtest_run_id).filter(
        StrategyProposal.session_id == session_id, StrategyProposal.backtest_run_id.isnot(None)).all()]
    StrategyProposal.query.filter_by(session_id=session_id).delete(synchronize_session=False)
    StrategyChatMessage.query.filter_by(session_id=session_id).delete(synchronize_session=False)
    if backtest_ids:
        BacktestRun.query.filter(BacktestRun.id.in_(backtest_ids), BacktestRun.strategies == "rule_dsl",
            ~db.exists().where(StrategyProposal.backtest_run_id == BacktestRun.id)).delete(synchronize_session=False)
    StrategyChatSession.query.filter_by(id=session_id).delete(synchronize_session=False)
    _audit(workspace_id, user.id, "strategy.chat_deleted", "strategy_chat_session", session_id)
    db.session.commit()
    return jsonify({"success":True})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategy-assistant/sessions/<int:session_id>", methods=["GET"])
def get_strategy_chat_session(workspace_id, session_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    chat_session = StrategyChatSession.query.filter_by(id=session_id, workspace_id=workspace_id).first_or_404()
    if not _session_strategy_access(chat_session, user, membership):
        return _error("没有此对话权限", 403, "chat_forbidden")
    definition = db.session.get(StrategyDefinition, chat_session.strategy_id) if chat_session.strategy_id else None
    limit = min(100, max(1, request.args.get("limit", 50, type=int)))
    before_id = request.args.get("before_message_id", type=int)
    query = StrategyChatMessage.query.filter_by(session_id=session_id)
    if before_id:
        query = query.filter(StrategyChatMessage.id < before_id)
    messages = query.order_by(StrategyChatMessage.id.desc()).limit(limit + 1).all()
    proposals = StrategyProposal.query.filter_by(session_id=session_id).order_by(StrategyProposal.id.desc()).limit(20).all()
    proposal_items = []
    for item in proposals:
        backtest = db.session.get(BacktestRun, item.backtest_run_id) if item.backtest_run_id else None
        backtest_data = json.loads(backtest.payload) if backtest and backtest.payload else None
        status = "expired" if item.expires_at and item.expires_at <= datetime.now() and item.status in {"validated", "backtested"} else item.status
        if status in {"validated", "backtested"} and definition and item.base_revision_id != definition.active_revision_id:
            status = "stale"
        proposal_items.append({"id": item.id, "status": status,
            "proposal": json.loads(item.proposed_spec_json), "diff": json.loads(item.diff_json or "{}"),
            "validation": json.loads(item.validation_report_json or "{}"), "base_revision_id": item.base_revision_id,
            "backtest_run_id": item.backtest_run_id,
            "backtest": {key: backtest_data.get(key) for key in ("region", "sample_count", "summary", "note")} if backtest_data else None})
    retry = _last_retryable_message(chat_session)
    return jsonify({"success": True, "session": {"id": chat_session.id, "title": chat_session.title,
            "status": chat_session.status, "can_edit": bool(chat_session.status == "active" and (not definition or definition.status == "active") and _session_strategy_access(chat_session, user, membership, edit=True))},
        "strategy": _chat_strategy_payload(definition),
        "messages": [_chat_message_payload(item) for item in reversed(messages[:limit])],
        "has_more_messages": len(messages) > limit, "proposals": proposal_items,
        "retry_message_id": retry.id if retry else None})


def _chat_failure(chat_session, user_message, message, status, code):
    db.session.add(StrategyChatMessage(session_id=chat_session.id, role="service", content=message))
    chat_session.updated_at = datetime.now()
    db.session.commit()
    return jsonify({"success": False, "error": code, "message": message,
                    "user_message_id": user_message.id}), status


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategy-assistant/sessions/<int:session_id>/messages", methods=["POST"])
def send_strategy_chat_message(workspace_id, session_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    chat_session = StrategyChatSession.query.filter_by(
        id=session_id,
        workspace_id=workspace_id,
        status="active",
    ).first_or_404()
    if not _session_strategy_access(chat_session, user, membership, edit=True):
        return _error("没有此策略对话权限", 403, "chat_forbidden")
    if rate_limited(f"strategy-assistant:user:{user.id}", 20, 300):
        return _error("策略助手调用过于频繁", 429, "rate_limited")
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    if not message or len(message) > 3000:
        return _error("消息长度必须为 1 到 3000 个字符")

    current_definition = (
        StrategyDefinition.query.get(chat_session.strategy_id)
        if chat_session.strategy_id else None
    )
    current_revision = (
        StrategyRevision.query.get(current_definition.active_revision_id)
        if current_definition and current_definition.active_revision_id else None
    )
    if "expected_strategy_id" in payload and payload["expected_strategy_id"] != (current_definition.public_id if current_definition else None):
        return _error("对话目标与当前策略不一致，请重新打开策略助手", 409, "chat_strategy_mismatch")
    if current_definition and current_definition.status != "active":
        return _error("策略已停用，此会话只可查看历史记录", 409, "strategy_unavailable")
    retry_id = payload.get("retry_message_id")
    if retry_id is not None:
        user_message = _last_retryable_message(chat_session)
        if type(retry_id) is not int or not user_message or user_message.id != retry_id or user_message.content != message:
            return _error("只能重试当前会话最后一条未获回复的消息", 409, "retry_conflict")
    else:
        first_message = not StrategyChatMessage.query.filter_by(session_id=session_id).first()
        user_message = StrategyChatMessage(session_id=session_id, role="user", content=message)
        db.session.add(user_message)
        if first_message:
            chat_session.title = message[:80]
    chat_session.updated_at = datetime.now()
    db.session.commit()

    from app import get_ai_config
    ai_config = get_ai_config()
    if not ai_config.get("api_key") or "你的" in str(ai_config.get("api_key") or ""):
        return _chat_failure(chat_session, user_message, "管理员尚未配置 AI 接口，请配置后重试。", 400, "ai_not_configured")

    history = StrategyChatMessage.query.filter_by(session_id=session_id).filter(StrategyChatMessage.role.in_(("user", "assistant"))).order_by(
        StrategyChatMessage.created_at.desc(), StrategyChatMessage.id.desc()
    ).limit(12).all()
    history.reverse()
    current_spec = json.loads(current_revision.spec_json) if current_revision else None
    system_prompt = (
        "你是策略规则助手。先与用户讨论需求、解释依据和限制，允许多轮澄清；只有适合形成明确规则时才提出草案。"
        "reply 必须是面向用户的完整中文回答，直接回应当前问题，不要只说已记录或已收到。"
        "有歧义的规则先提问；不要把尚未支持的规则描述为已实现，不要把只改窗口说成已实现排除或防重复。"
        "例如预防近期出现的生肖继续出现，需要澄清是排除近期生肖还是提高重复覆盖；这两者不同，不能默认为同一个参数。"
        "用户要求先讨论、不发布或暂不改动时，proposal 设为 null。已发布版本和待确认草案要明确区分。"
        "只可输出 JSON 对象，格式为 {\"reply\": string, \"proposal\": null 或 "
        "{\"name\": string, \"change_summary\": string, \"spec\": object}}。"
        "spec.schema_version 必须为 1，kind 仅支持 builtin_config、number_selection、zodiac_groups、external_selection。"
        "builtin_config 可引用 hot/cold/trend/hybrid/balanced/markov/ml，parameters 仅含 window(1-240)、pool(8-24)、special_pool(6-14)、trend_window(1-60)。"
        "builtin_config 引用 zodiac_rebound 时，parameters 仅含 window(10-240，默认20)和 parity_streak_threshold(2-20，默认5)，保留原六肖四肖算法。"
        "number_selection 支持 regions、window(1-240)、normal_count(1-20)、"
        "rank_metric(frequency/omission/special_frequency)、rank_order(highest/lowest)。"
        "zodiac_groups 支持 regions、window(1-240) 和 groups；每组含唯一 id、label、"
        "size(1-12)、metric(frequency/omission/special_frequency)、order(highest/lowest)。"
        "external_selection 仅能引用 source_key=macau_five_zodiac，source_fields 仅含 numbers/zodiacs。"
        "不得输出 Python、代码、URL、SQL、文件路径或新的数据源。不要发布策略，只提出草案。"
        "回测结果是历史统计，不代表未来概率。"
    )
    if current_definition:
        system_prompt += (
            "\n当前正在修改已有策略；用户从此策略的 AI 修改入口进入，不是在从零设计另一策略。"
            "先点明当前策略名称和输出，再回答其修改意图。沿用已给出的地区、输出类型和数量，不要反复询问已知配置。"
            "除非用户明确要求新增或改变输出，保留当前策略身份并作为新修订。历史中尚未关联策略时的讨论仅供参考，当前绑定规则为准。"
            f"策略身份：{current_definition.public_id}。"
        )
        if current_spec.get("strategy_key") == "zodiac_rebound":
            system_prompt += (
                "当前结果是生肖组合：综合六肖、精选四肖。算法先选遗漏靠前四肖，再补频次防守两肖，四肖在六肖内优先单双平衡。"
                "统计包括每期全部七个号码，同期重复生肖按号码计数，遗漏按特码生肖回看。"
                "不要擅自切换成号码预测；hot 与 number_selection 输出号码，并非此生肖策略的直接反向。"
                "用户明确改成40期热门方向时，可讨论改用 zodiac_groups、window=40、frequency/highest，沿用six大小6和four大小4及原地区。"
                "这会变更为按生肖频次排名，不仅是把遗漏回补窗口改成40；原四肖平衡等约束不会自动保留，须清楚说明差异。"
            )
        system_prompt += (
            f"\n当前策略名：{current_definition.name}。当前版本：{current_revision.id if current_revision else ''}。"
            f"当前规则：{json.dumps(current_spec, ensure_ascii=False)}。"
        )
    latest_proposal = StrategyProposal.query.filter(
        StrategyProposal.session_id == session_id,
        StrategyProposal.status.in_(("validated", "backtested")),
        StrategyProposal.base_revision_id == (current_revision.id if current_revision else None),
        db.or_(StrategyProposal.expires_at.is_(None), StrategyProposal.expires_at > datetime.now()),
    ).order_by(StrategyProposal.id.desc()).first()
    if latest_proposal:
        system_prompt += "\n最近待确认草案（尚未发布，用户可以继续讨论此版本）：" + json.dumps({
            "proposal_id": latest_proposal.id, "base_revision_id": latest_proposal.base_revision_id,
            "proposal": json.loads(latest_proposal.proposed_spec_json)}, ensure_ascii=False)
    request_payload = {
        "model": ai_config.get("model"),
        "messages": [{"role": "system", "content": system_prompt}] + [
            {"role": item.role, "content": item.content} for item in history
            if not (item.role == "assistant" and client_restriction(item.content))
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    headers = {
        "Authorization": f"Bearer {ai_config['api_key']}",
        "Content-Type": "application/json",
    }
    # Deleted SQLite rows can be recreated with the same integer IDs. Retain
    # the original record identity as well as the target while AI IO runs.
    reply_context = {"id": session_id, "workspace_id": workspace_id, "user_id": user.id,
                     "strategy_id": chat_session.strategy_id, "status": "active",
                     "created_at": chat_session.created_at}
    reply_message = (user_message.id, user_message.created_at, user_message.content)
    db.session.commit()
    def claim_reply_context():
        changed = StrategyChatSession.query.filter_by(**reply_context).update(
            {"updated_at": datetime.now()}, synchronize_session=False)
        persisted = db.session.get(StrategyChatMessage, reply_message[0], populate_existing=True) if changed else None
        if (not persisted or persisted.session_id != session_id or persisted.role != "user"
                or (persisted.id, persisted.created_at, persisted.content) != reply_message):
            db.session.rollback()
            return False
        db.session.expire_all()
        return True

    try:
        response = requests.post(ai_config["api_url"], json=request_payload, headers=headers, timeout=60)
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        ai_message = parse_assistant_content(content)
    except AssistantResponseError as exc:
        if not claim_reply_context():
            return _error("对话已删除或关联发生变化，回复未保存", 409, "chat_changed")
        return _chat_failure(chat_session, user_message, str(exc), 502, exc.code)
    except (KeyError, IndexError, TypeError, ValueError):
        if not claim_reply_context():
            return _error("对话已删除或关联发生变化，回复未保存", 409, "chat_changed")
        return _chat_failure(chat_session, user_message, "AI 服务返回了无法读取的回复，请重试或检查接口配置。", 502, "ai_response_invalid")
    except Exception:
        if not claim_reply_context():
            return _error("对话已删除或关联发生变化，回复未保存", 409, "chat_changed")
        return _chat_failure(chat_session, user_message, "AI 策略助手暂时不可用，请稍后重试。", 502, "ai_provider_error")

    if not claim_reply_context():
        return _error("对话已删除或关联发生变化，回复未保存", 409, "chat_changed")
    reply = str(ai_message.get("reply") or "").strip()
    proposal_data = ai_message.get("proposal")
    proposal_id = None
    public_proposal = None
    validation = {"valid": False, "errors": []}
    if isinstance(proposal_data, dict) and isinstance(proposal_data.get("spec"), dict):
        try:
            validate_strategy_spec(proposal_data["spec"])
            validation = {"valid": True, "errors": []}
            proposal = {
                "name": str(proposal_data.get("name") or (current_definition.name if current_definition else "新策略"))[:120],
                "change_summary": str(proposal_data.get("change_summary") or "AI 提出的规则调整")[:500],
                "spec": proposal_data["spec"],
            }
            public_proposal = proposal
            proposal_row = StrategyProposal(
                session_id=session_id,
                strategy_id=current_definition.id if current_definition else None,
                base_revision_id=current_revision.id if current_revision else None,
                proposed_spec_json=json.dumps(proposal, ensure_ascii=False, separators=(",", ":")),
                diff_json=json.dumps({"summary": proposal["change_summary"], "before": current_spec, "after": proposal["spec"]}, ensure_ascii=False),
                validation_report_json=json.dumps(validation, ensure_ascii=False),
                status="validated",
                expires_at=datetime.now() + timedelta(days=7),
                created_by_user_id=user.id,
            )
            StrategyProposal.query.filter(
                StrategyProposal.session_id == session_id,
                StrategyProposal.status.in_(("validated", "backtested")),
            ).update({"status": "superseded"}, synchronize_session=False)
            db.session.add(proposal_row)
            db.session.flush()
            proposal_id = proposal_row.id
        except ValueError as exc:
            validation = {"valid": False, "errors": [str(exc)]}
            if not reply:
                reply = "我整理了一个规则草案，但它没有通过可执行规则校验。请缩小规则范围后再试。"
    if not reply:
        reply = f"已整理草案“{public_proposal['name']}”：{public_proposal['change_summary']}。请检查具体规则，可以继续讨论或先回测；尚未发布。"
    db.session.add(StrategyChatMessage(session_id=session_id, role="assistant", content=reply))
    if validation.get("errors"):
        db.session.add(StrategyChatMessage(session_id=session_id, role="service",
            content="草案未通过规则校验，尚未保存或发布。请继续讨论并调整规则。原因：" + "；".join(validation["errors"])))
    chat_session.updated_at = datetime.now()
    db.session.commit()
    return jsonify({
        "success": True,
        "reply": reply,
        "user_message_id": user_message.id,
        "proposal_id": proposal_id,
        "proposal": public_proposal,
        "expected_revision_id": current_revision.id if current_revision else None,
        "validation": validation,
    })


def _proposal_access_error(proposal, user_id):
    # Workspace ownership does not expose private assistant conversations.
    if proposal.created_by_user_id != user_id:
        return _error("没有此草案权限", 403, "proposal_forbidden")
    if proposal.expires_at and proposal.expires_at <= datetime.now():
        return _error("草案已过期，请重新生成", 409, "proposal_expired")
    if proposal.status not in {"validated", "backtested"}:
        return _error("此草案已经结束，不能再次操作", 409, "proposal_terminal")
    return None


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategy-proposals/<int:proposal_id>/backtests", methods=["POST"])
def backtest_strategy_proposal(workspace_id, proposal_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    proposal_row = StrategyProposal.query.join(
        StrategyChatSession,
        StrategyProposal.session_id == StrategyChatSession.id,
    ).filter(
        StrategyProposal.id == proposal_id,
        StrategyChatSession.workspace_id == workspace_id,
    ).first_or_404()
    error = _proposal_access_error(proposal_row, user.id)
    if error:
        return error
    if rate_limited(f"strategy-backtest:user:{user.id}", 10, 3600):
        return _error("策略回测请求过于频繁", 429, "rate_limited")
    proposal = json.loads(proposal_row.proposed_spec_json)
    spec = proposal["spec"]
    if spec.get("kind") == "external_selection":
        return _error("外部来源策略须有可证明的逐期历史快照后才能回测", 422, "source_backtest_unavailable")

    from app import (
        _get_number_to_zodiac_map,
        _infer_draw_year,
        _load_backtest_draws_from_db,
        _normalize_backtest_draws,
    )
    from models import ZodiacSetting

    region = str((request.get_json(silent=True) or {}).get("region") or "hk").strip()
    if region not in {"hk", "macau"} or region not in spec.get("regions", ["hk", "macau"]):
        return _error("草案不支持所选地区")
    draws = _normalize_backtest_draws(_load_backtest_draws_from_db(region), limit=240)
    outcomes = []
    failures = []
    started = time.monotonic()
    source_definition = StrategyDefinition.query.get(proposal_row.strategy_id) if proposal_row.strategy_id else None
    definition_key = source_definition.public_id if source_definition else f"proposal-{proposal_row.id}"
    for target_index in range(1, len(draws)):
        if time.monotonic() - started > 30:
            return _error("回测超过执行预算，请缩小参数窗口", 422, "backtest_budget_exceeded")
        target = draws[target_index]
        history = list(reversed(draws[:target_index]))
        try:
            result = execute_workspace_spec(
                spec,
                history,
                region,
                target.get("id"),
                definition_key,
                backtest=True, target_date=target.get("date"),
            )
        except ValueError:
            failures.append({"period": str(target.get("id") or ""), "error_code": "insufficient_or_invalid_input"})
            continue
        target_map = _get_number_to_zodiac_map(ZodiacSetting.get_zodiac_year_for_date(target.get("date")))
        try:
            metrics = evaluate_result(result, target, target_map)
        except ValueError:
            failures.append({"period": str(target.get("id") or ""), "error_code": "insufficient_or_invalid_input"})
            continue
        metrics["input_cutoff"] = str(history[0].get("id") or "")
        outcomes.append(metrics)
    summary = {}
    number_outcomes = [item for item in outcomes if "special_hit" in item]
    if number_outcomes:
        summary["special"] = {
            "hits": sum(bool(item["special_hit"]) for item in number_outcomes),
            "total": len(number_outcomes),
            "hit_rate": round(100 * sum(bool(item["special_hit"]) for item in number_outcomes) / len(number_outcomes), 2),
            "random_baseline": round(100 * sum(item["special_random_baseline"] for item in number_outcomes) / len(number_outcomes), 2),
        }
        summary["normal_coverage"] = {
            "hits": sum(bool(item["normal_hit"]) for item in number_outcomes),
            "total": len(number_outcomes),
            "hit_rate": round(100 * sum(bool(item["normal_hit"]) for item in number_outcomes) / len(number_outcomes), 2),
            "random_baseline": round(100 * sum(item["normal_random_baseline"] for item in number_outcomes) / len(number_outcomes), 2),
        }
    group_ids = sorted({group_id for item in outcomes for group_id in (item.get("groups") or {})})
    summary["groups"] = {}
    for group_id in group_ids:
        sample = [item["groups"][group_id] for item in outcomes if group_id in (item.get("groups") or {})]
        summary["groups"][group_id] = {
            "hits": sum(bool(item["hit"]) for item in sample),
            "total": len(sample),
            "hit_rate": round(100 * sum(bool(item["hit"]) for item in sample) / len(sample), 2),
            "random_baseline": round(100 * sum(item["random_baseline"] for item in sample) / len(sample), 2),
        }
    payload = {
        "schema_version": 1,
        "engine_version": ENGINE_VERSION,
        "proposal_id": proposal_row.id,
        "strategy_id": source_definition.public_id if source_definition else None,
        "revision_id": proposal_row.base_revision_id,
        "spec": spec,
        "runtime_ms": round((time.monotonic() - started) * 1000),
        "errors": failures,
        "region": region,
        "sample_count": len(outcomes),
        "summary": summary,
        "results": outcomes,
        "note": "仅按每个目标期之前的数据计算；历史命中率不代表未来结果。",
    }
    if not outcomes:
        return _error("现有开奖历史不足以完成该规则的回测", 422, "backtest_history_insufficient")
    backtest = BacktestRun(
        name=f"策略草案 {proposal.get('name', '')}"[:120],
        region=region,
        strategies="rule_dsl",
        periods_evaluated=len(outcomes),
        payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    db.session.add(backtest)
    db.session.flush()
    claimed = StrategyProposal.query.filter(
        StrategyProposal.id == proposal_row.id,
        StrategyProposal.status.in_(("validated", "backtested")),
    ).update({"status": "backtested", "backtest_run_id": backtest.id}, synchronize_session=False)
    if not claimed:
        db.session.rollback()
        return _error("草案状态已变化，请重新载入", 409, "proposal_terminal")
    proposal_row.backtest_run_id = backtest.id
    proposal_row.status = "backtested"
    proposal_row.validation_report_json = json.dumps({
        "valid": True,
        "backtest": {"run_id": backtest.id, "sample_count": len(outcomes)},
    }, ensure_ascii=False)
    _audit(workspace_id, user.id, "strategy.proposal_backtested", "strategy_proposal", proposal_row.id,
           {"backtest_run_id": backtest.id, "sample_count": len(outcomes)})
    db.session.commit()
    return jsonify({"success": True, "backtest": payload, "backtest_run_id": backtest.id})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategy-proposals/<int:proposal_id>/publish", methods=["POST"])
def publish_strategy_proposal(workspace_id, proposal_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    proposal_row = StrategyProposal.query.join(
        StrategyChatSession,
        StrategyProposal.session_id == StrategyChatSession.id,
    ).filter(
        StrategyProposal.id == proposal_id,
        StrategyChatSession.workspace_id == workspace_id,
    ).first_or_404()
    error = _proposal_access_error(proposal_row, user.id)
    if error:
        return error
    if (request.get_json(silent=True) or {}).get("confirm") is not True:
        return _error("请明确确认发布此草案", 400, "confirmation_required")
    if proposal_row.status != "backtested":
        return _error("草案需要先通过校验和回测", 409, "proposal_not_ready")
    proposal = json.loads(proposal_row.proposed_spec_json)
    spec = proposal["spec"]
    validate_strategy_spec(spec)
    payload = request.get_json(silent=True) or {}
    definition = StrategyDefinition.query.get(proposal_row.strategy_id) if proposal_row.strategy_id else None
    if definition:
        if definition.status != "active" or definition.workspace_id != workspace_id:
            return _error("策略已停用，不能发布旧草案", 409, "strategy_unavailable")
        if payload.get("expected_revision_id") != proposal_row.base_revision_id:
            return _error("策略版本已更新，请重新生成草案", 409, "revision_conflict")
        if definition.active_revision_id != proposal_row.base_revision_id:
            return _error("策略版本已更新，请重新生成草案", 409, "revision_conflict")
        if not _editable_strategy(definition, membership, user.id):
            return _error("没有此策略发布权限", 403, "strategy_forbidden")
        revision_no = StrategyRevision.query.filter_by(strategy_id=definition.id).count() + 1
    claimed = StrategyProposal.query.filter_by(id=proposal_row.id, status="backtested").update(
        {"status": "publishing"}, synchronize_session=False,
    )
    if not claimed:
        db.session.rollback()
        return _error("草案状态已变化，请重新载入", 409, "proposal_terminal")
    if definition is None:
        public_id = str(uuid.uuid4())
        slug = f"strategy-{public_id[:8]}"
        definition = StrategyDefinition(
            public_id=public_id,
            workspace_id=workspace_id,
            owner_user_id=user.id,
            slug=slug,
            name=proposal["name"],
            kind="builtin_config" if spec.get("kind") == "builtin_config" else "rule_dsl",
            visibility="private",
            status="active",
            created_by_user_id=user.id,
        )
        db.session.add(definition)
        db.session.flush()
        revision_no = 1
    revision = StrategyRevision(
        strategy_id=definition.id,
        revision_no=revision_no,
        parent_revision_id=definition.active_revision_id if revision_no > 1 else None,
        schema_version=1,
        spec_json=json.dumps(spec, ensure_ascii=False, separators=(",", ":")),
        source="ai_proposal",
        change_summary=proposal["change_summary"],
        created_by_user_id=user.id,
    )
    db.session.add(revision)
    db.session.flush()
    definition.active_revision_id = revision.id
    definition.status = "active"
    proposal_row.strategy_id = definition.id
    chat_session = db.session.get(StrategyChatSession, proposal_row.session_id)
    if chat_session and chat_session.user_id == user.id and chat_session.strategy_id is None:
        chat_session.strategy_id = definition.id
        chat_session.updated_at = datetime.now()
    proposal_row.status = "published"
    StrategyProposal.query.filter(StrategyProposal.session_id == proposal_row.session_id,
        StrategyProposal.id != proposal_row.id, StrategyProposal.status.in_(("validated", "backtested"))).update(
            {"status": "superseded"}, synchronize_session=False)
    _audit(workspace_id, user.id, "strategy.proposal_published", "strategy", definition.public_id,
           {"proposal_id": proposal_id, "revision_id": revision.id})
    _enqueue_strategy_backup(workspace_id, definition, revision, user.id)
    db.session.commit()
    return jsonify({"success": True, "item": _strategy_payload(definition, revision)})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/management/backup/github", methods=["GET"])
def get_github_backup_settings(workspace_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    destinations = StrategyBackupDestination.query.filter_by(workspace_id=workspace_id).all()
    outbox_counts = {}
    for status, count in db.session.query(
        StrategyBackupOutbox.status,
        func.count(StrategyBackupOutbox.id),
    ).filter(StrategyBackupOutbox.workspace_id == workspace_id).group_by(StrategyBackupOutbox.status).all():
        outbox_counts[status] = int(count)
    return jsonify({
        "success": True,
        "token_configured": bool(str(os.environ.get("STRATEGY_GITHUB_TOKEN") or "").strip()),
        "destinations": [{
            "id": item.id,
            "provider": item.provider,
            "repository": item.repository_ref,
            "branch": item.branch,
            "enabled": item.enabled,
        } for item in destinations],
        "outbox_counts": outbox_counts,
    })


@strategy_api_bp.route("/workspaces/<int:workspace_id>/management/backup/github", methods=["POST"])
def configure_github_backup(workspace_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    repository = str(payload.get("repository") or "").strip()
    branch = str(payload.get("branch") or "main").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return _error("repository 必须使用 owner/name 格式")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", branch) or ".." in branch:
        return _error("branch 名称无效")
    token = str(os.environ.get("STRATEGY_GITHUB_TOKEN") or "").strip()
    if not token:
        return _error("请在服务器环境变量 STRATEGY_GITHUB_TOKEN 配置私有仓库令牌", 503, "github_token_missing")
    try:
        verify_private_repository(repository, token)
    except ValueError as exc:
        return _error(str(exc), 422, "github_repository_invalid")
    except Exception:
        return _error("无法连接 GitHub，请稍后重试", 502, "github_unavailable")
    destination = StrategyBackupDestination.query.filter_by(
        workspace_id=workspace_id,
        provider="github",
        repository_ref=repository,
    ).first()
    if destination is None:
        destination = StrategyBackupDestination(
            workspace_id=workspace_id,
            provider="github",
            repository_ref=repository,
            path_prefix="strategies",
            branch=branch,
            credential_ref="STRATEGY_GITHUB_TOKEN",
            enabled=True,
            created_by_user_id=user.id,
        )
        db.session.add(destination)
    else:
        destination.branch = branch
        destination.enabled = True
    _audit(workspace_id, user.id, "backup.destination_configured", "backup_destination", repository)
    db.session.commit()
    return jsonify({"success": True, "destination_id": destination.id, "repository": repository})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/management/backup/github/sync", methods=["POST"])
def queue_github_strategy_sync(workspace_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    destinations = StrategyBackupDestination.query.filter_by(
        workspace_id=workspace_id,
        provider="github",
        enabled=True,
    ).all()
    if not destinations:
        return _error("请先配置私有 GitHub 仓库", 409, "github_destination_missing")
    definitions = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
        status="active",
    ).all()
    queued = 0
    for definition in definitions:
        if not definition.active_revision_id:
            continue
        for destination in destinations:
            existing = StrategyBackupOutbox.query.filter_by(
                destination_id=destination.id,
                revision_id=definition.active_revision_id,
            ).first()
            if existing is None:
                db.session.add(StrategyBackupOutbox(
                    workspace_id=workspace_id,
                    strategy_id=definition.id,
                    revision_id=definition.active_revision_id,
                    destination_id=destination.id,
                    status="pending",
                ))
                queued += 1
    _audit(workspace_id, user.id, "backup.sync_requested", "workspace", workspace_id,
           {"queued": queued})
    db.session.commit()
    return jsonify({"success": True, "queued": queued})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/management/backup/github/<int:destination_id>", methods=["DELETE"])
def disable_github_backup(workspace_id, destination_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    destination = StrategyBackupDestination.query.filter_by(
        id=destination_id,
        workspace_id=workspace_id,
        provider="github",
    ).first_or_404()
    destination.enabled = False
    _audit(workspace_id, user.id, "backup.destination_disabled", "backup_destination", destination_id)
    db.session.commit()
    return jsonify({"success": True})


def _read_github_strategy_bundle(destination, path):
    token = str(os.environ.get("STRATEGY_GITHUB_TOKEN") or "").strip()
    if not token:
        raise ValueError("服务器没有配置 STRATEGY_GITHUB_TOKEN")
    normalized_path = str(path or "").strip().lstrip("/")
    workspace = db.session.get(StrategyWorkspace, destination.workspace_id)
    required_prefix = f"{str(destination.path_prefix or 'strategies').strip('/')}/workspaces/{workspace.public_id}/"
    parts = normalized_path.split("/")
    if (not normalized_path.startswith(required_prefix)
            or len(parts) < 7
            or ".." in parts
            or parts[-1].count(".") != 1
            or not parts[-1].endswith(".json")):
        raise ValueError("备份文件路径不属于策略仓库目录")
    owner, repository = destination.repository_ref.split("/", 1)
    response = requests.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repository, safe='')}/contents/{quote(normalized_path, safe='/')}",
        params={"ref": destination.branch},
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise ValueError("GitHub 备份文件不可用")
    response_data = response.json()
    if response_data.get("encoding") != "base64":
        raise ValueError("GitHub 返回的备份文件格式无效")
    encoded = str(response_data.get("content") or "").replace("\n", "")
    if len(encoded) > 500000:
        raise ValueError("策略备份文件过大")
    bundle = json.loads(base64.b64decode(encoded).decode("utf-8"))
    return validate_bundle(bundle)



@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies/<string:public_id>/export", methods=["GET"])
def export_workspace_strategy(workspace_id, public_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author", "member"})
    if error:
        return error
    definition = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
        public_id=public_id,
        status="active",
    ).first_or_404()
    if not _visible_strategy(definition, membership, user.id):
        return _error("没有此策略导出权限", 403, "strategy_forbidden")
    if definition.owner_user_id != user.id and membership.role != "owner":
        return _error("只有策略作者或 workspace owner 可以导出策略", 403, "strategy_export_forbidden")
    revision = StrategyRevision.query.get(definition.active_revision_id)
    if revision is None:
        return _error("策略没有有效版本", 409, "revision_unavailable")
    workspace = StrategyWorkspace.query.get(workspace_id)
    try:
        bundle = build_bundle(definition, revision)
    except ValueError:
        return _error("策略包含敏感文本，清理后才能导出", 422, "backup_sensitive_content")
    _audit(workspace_id, user.id, "strategy.exported", "strategy", public_id)
    db.session.commit()
    return jsonify({"success": True, "bundle": bundle})


def _import_strategy_bundle(workspace_id, user, bundle, source):
    strategy = bundle["strategy"]
    public_id = str(uuid.uuid4())
    definition = StrategyDefinition(public_id=public_id, workspace_id=workspace_id,
        owner_user_id=user.id, slug=f"imported-{public_id[:8]}", name=strategy["name"],
        kind=strategy["kind"], visibility="private", status="draft", created_by_user_id=user.id)
    db.session.add(definition)
    db.session.flush()
    revision = StrategyRevision(strategy_id=definition.id, revision_no=1, schema_version=1,
        spec_json=canonical_json(strategy["spec"]), source=source, change_summary="从策略包导入为草稿",
        engine_compatibility=strategy["engine_compatibility"], created_by_user_id=user.id)
    db.session.add(revision)
    db.session.flush()
    _audit(workspace_id, user.id, "strategy.imported_as_draft", "strategy", public_id, {"digest": bundle["digest"]})
    db.session.commit()
    return jsonify({"success": True, "item": _strategy_payload(definition, revision)}), 201


@strategy_api_bp.route("/workspaces/<int:workspace_id>/management/backup/import-preview", methods=["POST"])
def preview_local_strategy_import(workspace_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    try:
        bundle = validate_bundle((request.get_json(silent=True) or {}).get("bundle"))
    except (ValueError, TypeError):
        return _error("策略包校验失败，请检查摘要、版本和规则", 422, "invalid_strategy_bundle")
    strategy = bundle["strategy"]
    existing = StrategyDefinition.query.filter_by(workspace_id=workspace_id, public_id=strategy["id"]).first()
    existing_revision = db.session.get(StrategyRevision, existing.active_revision_id) if existing and existing.active_revision_id else None
    return jsonify({"success": True, "digest": bundle["digest"], "preview": strategy,
                    "diff": {"before": json.loads(existing_revision.spec_json) if existing_revision else None,
                             "after": strategy["spec"]}, "import_status": "draft"})


@strategy_api_bp.route("/workspaces/<int:workspace_id>/management/backup/import", methods=["POST"])
def import_local_strategy(workspace_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        bundle = validate_bundle(payload.get("bundle"))
    except (ValueError, TypeError):
        return _error("策略包校验失败，请检查摘要、版本和规则", 422, "invalid_strategy_bundle")
    if payload.get("confirm") is not True or payload.get("expected_digest") != bundle["digest"]:
        return _error("请先预览并确认同一策略包", 409, "import_confirmation_required")
    return _import_strategy_bundle(workspace_id, user, bundle, "bundle_import")


@strategy_api_bp.route("/workspaces/<int:workspace_id>/management/backup/github/import-preview", methods=["POST"])
def preview_github_strategy_import(workspace_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    destination = StrategyBackupDestination.query.filter_by(
        id=payload.get("destination_id"),
        workspace_id=workspace_id,
        provider="github",
    ).first()
    if destination is None:
        return _error("备份目标不存在", 404, "backup_destination_missing")
    try:
        bundle = _read_github_strategy_bundle(destination, payload.get("path"))
    except ValueError as exc:
        return _error(str(exc), 422, "invalid_strategy_bundle")
    except Exception:
        return _error("无法读取 GitHub 策略文件", 502, "github_unavailable")
    strategy = bundle["strategy"]
    return jsonify({
        "success": True,
        "digest": bundle["digest"],
        "preview": {
            "name": strategy.get("name"),
            "slug": strategy.get("slug"),
            "kind": strategy.get("kind"),
            "revision_no": strategy.get("revision_no"),
            "spec": strategy["spec"],
        },
    })


@strategy_api_bp.route("/workspaces/<int:workspace_id>/management/backup/github/import", methods=["POST"])
def import_github_strategy(workspace_id):
    user, error = _active_user()
    if error:
        return error
    _, error = _require_role(workspace_id, user.id, {"owner"})
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    destination = StrategyBackupDestination.query.filter_by(
        id=payload.get("destination_id"),
        workspace_id=workspace_id,
        provider="github",
    ).first()
    if destination is None:
        return _error("备份目标不存在", 404, "backup_destination_missing")
    try:
        bundle = _read_github_strategy_bundle(destination, payload.get("path"))
    except ValueError as exc:
        return _error(str(exc), 422, "invalid_strategy_bundle")
    except Exception:
        return _error("无法读取 GitHub 策略文件", 502, "github_unavailable")
    if payload.get("confirm") is not True or payload.get("expected_digest") != bundle["digest"]:
        return _error("请先预览并确认同一策略包", 409, "import_confirmation_required")
    return _import_strategy_bundle(workspace_id, user, bundle, "github_import")



@strategy_api_bp.route("/workspaces/<int:workspace_id>/strategies/<string:public_id>/publish-draft", methods=["POST"])
def publish_imported_strategy_draft(workspace_id, public_id):
    user, error = _active_user()
    if error:
        return error
    membership, error = _require_role(workspace_id, user.id, {"owner", "strategy_author"})
    if error:
        return error
    definition = StrategyDefinition.query.filter_by(
        workspace_id=workspace_id,
        public_id=public_id,
        status="draft",
    ).first_or_404()
    if not _editable_strategy(definition, membership, user.id):
        return _error("没有此策略发布权限", 403, "strategy_forbidden")
    revision = StrategyRevision.query.filter_by(strategy_id=definition.id).order_by(
        StrategyRevision.revision_no.desc()
    ).first()
    if revision is None:
        return _error("草稿没有策略规则", 409, "revision_unavailable")
    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") is not True:
        return _error("请确认发布草稿", 400, "confirmation_required")
    if payload.get("expected_revision_id") != revision.id:
        return _error("草稿版本已变化", 409, "revision_conflict")
    validate_strategy_spec(json.loads(revision.spec_json))
    definition.active_revision_id = revision.id
    definition.status = "active"
    _audit(workspace_id, user.id, "strategy.imported_draft_published", "strategy", definition.public_id,
           {"revision_id": revision.id})
    _enqueue_strategy_backup(workspace_id, definition, revision, user.id)
    db.session.commit()
    return jsonify({"success": True, "item": _strategy_payload(definition, revision)})
