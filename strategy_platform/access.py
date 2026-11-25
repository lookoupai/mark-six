"""Workspace role lookups shared by the web and API layers."""

from models import StrategyWorkspace, StrategyWorkspaceMembership, User, db


def get_default_workspace_for_owner(user):
    if not getattr(user, "is_admin", False):
        return None
    owner_user = User.query.filter(
        User.is_admin.is_(True),
        User.is_active.is_(True),
    ).order_by(User.id.asc()).first()
    if owner_user is None:
        return None
    workspace = StrategyWorkspace.query.filter_by(slug="friends").first()
    if workspace is None:
        workspace = StrategyWorkspace(
            slug="friends",
            name="策略工作区",
            status="active",
        )
        db.session.add(workspace)
        db.session.flush()

    owner = StrategyWorkspaceMembership.query.filter_by(
        workspace_id=workspace.id,
        role="owner",
        status="active",
    ).first()
    if owner is None:
        membership = StrategyWorkspaceMembership.query.filter_by(
            workspace_id=workspace.id,
            user_id=owner_user.id,
        ).first()
        if membership is None:
            membership = StrategyWorkspaceMembership(
                workspace_id=workspace.id,
                user_id=owner_user.id,
                role="owner",
                status="active",
            )
            db.session.add(membership)
        else:
            membership.role = "owner"
            membership.status = "active"
        db.session.commit()
    return workspace


def get_workspace_membership(workspace_id, user_id):
    return StrategyWorkspaceMembership.query.join(
        StrategyWorkspace, StrategyWorkspace.id == StrategyWorkspaceMembership.workspace_id,
    ).filter(
        StrategyWorkspaceMembership.workspace_id == workspace_id,
        StrategyWorkspaceMembership.user_id == user_id,
        StrategyWorkspaceMembership.status == "active",
        StrategyWorkspace.status == "active",
    ).first()


def user_has_workspace_role(workspace_id, user_id, roles):
    membership = get_workspace_membership(workspace_id, user_id)
    return bool(membership and membership.role in set(roles))



def rename_workspace(workspace, actor_id, name, expected_name=None):
    """Rename display metadata without changing stable identity or membership."""
    from models import StrategyAuditEvent
    import json

    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name):
        raise ValueError("工作区名称须为 1 到 120 个可见字符")
    previous = workspace.name
    if expected_name is not None and expected_name != previous:
        return False
    name = name.strip()
    if name == previous:
        return True
    changed = StrategyWorkspace.query.filter_by(id=workspace.id, name=previous, status="active").update(
        {"name": name}, synchronize_session=False)
    if not changed:
        return False
    db.session.add(StrategyAuditEvent(workspace_id=workspace.id, actor_user_id=actor_id,
        action="workspace.renamed", resource_type="workspace", resource_id=str(workspace.id),
        details_json=json.dumps({"before": previous, "after": name}, ensure_ascii=False)))
    db.session.expire(workspace)
    return True
