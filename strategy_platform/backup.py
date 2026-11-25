"""Private GitHub backup processing for published strategy revisions."""

import base64
import json
import os
import re
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

from models import (
    StrategyBackupDestination,
    StrategyBackupOutbox,
    StrategyDefinition,
    StrategyRevision,
    StrategyWorkspace,
    db,
)


_GITHUB_API = "https://api.github.com"
_GITHUB_ACCEPT = "application/vnd.github+json"
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _github_headers(token):
    return {
        "Accept": _GITHUB_ACCEPT,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def verify_private_repository(repository_ref, token):
    if not _REPOSITORY_RE.fullmatch(str(repository_ref or "")):
        raise ValueError("repository must use owner/name format")
    owner, repository = repository_ref.split("/", 1)
    response = requests.get(
        f"{_GITHUB_API}/repos/{quote(owner, safe='')}/{quote(repository, safe='')}",
        headers=_github_headers(token),
        timeout=15, allow_redirects=False,
    )
    if response.status_code != 200:
        raise requests.RequestException("GitHub repository is temporarily unavailable")
    if response.json().get("private") is not True:
        raise ValueError("strategy backups require a private GitHub repository")
    return response.json()


def _revision_document(workspace, definition, revision):
    from strategy_platform.bundles import build_bundle
    return build_bundle(definition, revision)


def _backup_path(destination, workspace, definition, revision, outbox):
    prefix = str(destination.path_prefix or "strategies").strip("/")
    return (f"{prefix}/workspaces/{workspace.public_id}/strategies/{definition.public_id}/"
            f"{outbox.event_type}/r{revision.revision_no}-event-{outbox.id}.json")


def _retry(outbox, code):
    outbox.status = "pending"
    outbox.last_error_code = code
    outbox.attempt_count += 1
    outbox.retry_at = datetime.now() + timedelta(minutes=min(360, 2 ** min(outbox.attempt_count, 8)))


def _deliver(outbox):
    destination = db.session.get(StrategyBackupDestination, outbox.destination_id)
    definition = db.session.get(StrategyDefinition, outbox.strategy_id)
    revision = db.session.get(StrategyRevision, outbox.revision_id)
    workspace = db.session.get(StrategyWorkspace, outbox.workspace_id)
    token = str(os.environ.get("STRATEGY_GITHUB_TOKEN") or "").strip()
    if not destination or not definition or not revision or not workspace:
        outbox.status, outbox.last_error_code = "failed", "backup_configuration_missing"
        return
    if not destination.enabled:
        _retry(outbox, "backup_destination_disabled")
        return
    if not token:
        _retry(outbox, "github_token_not_configured")
        return
    try:
        verify_private_repository(destination.repository_ref, token)
        owner, repository = destination.repository_ref.split("/", 1)
        path = _backup_path(destination, workspace, definition, revision, outbox)
        if not outbox.payload_json:
            from strategy_platform.bundles import canonical_json
            outbox.payload_json = canonical_json(_revision_document(workspace, definition, revision))
        content = outbox.payload_json.encode("utf-8")
        url = f"{_GITHUB_API}/repos/{quote(owner, safe='')}/{quote(repository, safe='')}/contents/{quote(path, safe='/')}"
        response = requests.put(url, headers={**_github_headers(token), "Content-Type": "application/json"},
                                json={"message": f"Backup strategy {definition.public_id} revision {revision.revision_no}",
                                      "content": base64.b64encode(content).decode("ascii"), "branch": destination.branch},
                                timeout=30, allow_redirects=False)
        if response.status_code in {200, 201}:
            outbox.status = "synced"
            outbox.last_error_code, outbox.retry_at = None, None
            payload = response.json()
            outbox.remote_reference = str((payload.get("commit") or {}).get("html_url") or "")[:255]
            return
        if response.status_code in {409, 422}:
            # A previous write may have succeeded before the response was lost.
            existing = requests.get(url, params={"ref": destination.branch}, headers=_github_headers(token),
                                    timeout=30, allow_redirects=False)
            if existing.status_code == 200:
                remote = existing.json()
                if remote.get("encoding") == "base64" and len(str(remote.get("content") or "")) <= 150000:
                    raw = base64.b64decode(str(remote.get("content") or "").replace("\n", ""), validate=True)
                    if raw == content:
                        outbox.status = "synced"
                        outbox.last_error_code, outbox.retry_at = None, None
                        outbox.remote_reference = str(remote.get("html_url") or "")[:255]
                        return
                outbox.status, outbox.last_error_code = "failed", "github_content_conflict"
                return
        _retry(outbox, f"github_http_{response.status_code}")
    except requests.RequestException:
        _retry(outbox, "github_request_failed")
    except ValueError:
        outbox.status, outbox.last_error_code = "failed", "github_invalid_repository_or_content"


def process_strategy_backup_outbox(limit=5):
    now = datetime.now()
    rows = StrategyBackupOutbox.query.filter(
        StrategyBackupOutbox.status == "pending",
        db.or_(StrategyBackupOutbox.retry_at.is_(None), StrategyBackupOutbox.retry_at <= now),
    ).order_by(StrategyBackupOutbox.created_at.asc(), StrategyBackupOutbox.id.asc()).limit(limit).all()
    for outbox in rows:
        _deliver(outbox)
        # Never retain a previous row's UPDATE lock during the next network IO.
        db.session.commit()
    return len(rows)
