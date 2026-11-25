"""Workspace-scoped access to registered external prediction snapshots."""

import json
import hashlib

from models import (
    MacauCollectedData,
    StrategyWorkspace,
    StrategyWorkspaceMembership,
    StrategySourceDefinition,
    StrategySourceSnapshot,
    db,
)

_ZODIACS = tuple("鼠牛虎兔龙蛇马羊猴鸡狗猪")


def load_strategy_source_snapshot(workspace_id, source_key, region, period, user_id):
    if source_key != "macau_five_zodiac":
        raise ValueError("external source is not registered")
    if region != "macau":
        raise ValueError("五码五肖数据仅适用于澳门地区")

    source = StrategySourceDefinition.query.filter_by(
        workspace_id=workspace_id,
        source_key=source_key,
    ).first()
    if source is None:
        source = StrategySourceDefinition(
            workspace_id=workspace_id,
            source_key=source_key,
            name="澳门五码五肖采集",
            provider="existing_macau_collection",
            parser_version="legacy-collector-v1",
            status="active",
            created_by_user_id=user_id,
        )
        db.session.add(source)
        db.session.flush()

    snapshot = StrategySourceSnapshot.query.filter_by(
        source_id=source.id,
        region=region,
        period=str(period),
    ).order_by(StrategySourceSnapshot.fetched_at.desc(), StrategySourceSnapshot.id.desc()).first()
    if snapshot is None:
        record = MacauCollectedData.query.filter_by(region=region, period=str(period)).first()
        if record is None:
            raise ValueError("该期没有可用的五码五肖来源数据")
        numbers = [value.strip() for value in str(record.numbers or "").split(",") if value.strip()]
        zodiacs = [value.strip() for value in str(record.zodiacs or "").split(",") if value.strip()]
        numbers_valid = len(numbers) == 5 and all(
            value.isdigit() and 1 <= int(value) <= 49 for value in numbers
        ) and len({int(value) for value in numbers}) == 5
        zodiacs_valid = len(zodiacs) == 5 and len(set(zodiacs)) == 5 and all(value in _ZODIACS for value in zodiacs)
        snapshot = StrategySourceSnapshot(
            source_id=source.id,
            region=region,
            period=str(period),
            published_at=None,
            fetched_at=record.updated_at or record.created_at,
            parser_version=source.parser_version,
            normalized_payload_json=json.dumps(
                {"numbers": numbers, "zodiacs": zodiacs},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            quality_status="accepted" if numbers_valid and zodiacs_valid else "rejected",
            quality_report_json=json.dumps(
                {"numbers_count": len(numbers), "zodiacs_count": len(zodiacs),
                 "numbers_valid": numbers_valid, "zodiacs_valid": zodiacs_valid},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        db.session.add(snapshot)
        db.session.flush()

    if source.status != "active":
        raise ValueError("外部来源已停用")
    if snapshot.quality_status != "accepted":
        raise ValueError("外部来源数据未通过质量检查")
    return {
        "id": snapshot.id,
        "period": snapshot.period,
        "published_at": snapshot.published_at.isoformat() if snapshot.published_at else None,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "parser_version": snapshot.parser_version,
        "quality_status": snapshot.quality_status,
        "sha256": hashlib.sha256(snapshot.normalized_payload_json.encode()).hexdigest(),
        "payload": json.loads(snapshot.normalized_payload_json),
    }


def capture_macau_collection_snapshots(records):
    workspaces = StrategyWorkspace.query.filter_by(status="active").all()
    if not workspaces:
        return 0
    captured = 0
    for workspace in workspaces:
        owner = StrategyWorkspaceMembership.query.filter_by(
            workspace_id=workspace.id,
            role="owner",
            status="active",
        ).first()
        if owner is None:
            continue
        source = StrategySourceDefinition.query.filter_by(
            workspace_id=workspace.id,
            source_key="macau_five_zodiac",
        ).first()
        if source is None:
            source = StrategySourceDefinition(
                workspace_id=workspace.id,
                source_key="macau_five_zodiac",
                name="澳门五码五肖采集",
                provider="existing_macau_collection",
                parser_version="legacy-collector-v1",
                status="active",
                created_by_user_id=owner.user_id,
            )
            db.session.add(source)
            db.session.flush()
        for record in records:
            numbers = [value.strip() for value in str(record.numbers or "").split(",") if value.strip()]
            zodiacs = [value.strip() for value in str(record.zodiacs or "").split(",") if value.strip()]
            payload = {"numbers": numbers, "zodiacs": zodiacs}
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            latest = StrategySourceSnapshot.query.filter_by(
                source_id=source.id,
                region=record.region,
                period=record.period,
            ).order_by(StrategySourceSnapshot.fetched_at.desc(), StrategySourceSnapshot.id.desc()).first()
            if latest and latest.normalized_payload_json == serialized:
                continue
            numbers_valid = len(numbers) == 5 and all(
                value.isdigit() and 1 <= int(value) <= 49 for value in numbers
            ) and len({int(value) for value in numbers}) == 5
            zodiacs_valid = len(zodiacs) == 5 and len(set(zodiacs)) == 5 and all(value in _ZODIACS for value in zodiacs)
            db.session.add(StrategySourceSnapshot(
                source_id=source.id,
                region=record.region,
                period=record.period,
                published_at=None,
                fetched_at=record.updated_at or record.created_at,
                parser_version=source.parser_version,
                normalized_payload_json=serialized,
                quality_status="accepted" if numbers_valid and zodiacs_valid else "rejected",
                quality_report_json=json.dumps({
                    "numbers_count": len(numbers),
                    "zodiacs_count": len(zodiacs),
                    "numbers_valid": numbers_valid,
                    "zodiacs_valid": zodiacs_valid,
                }, ensure_ascii=False, separators=(",", ":")),
            ))
            captured += 1
    return captured
