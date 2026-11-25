"""Destructive upgrade rehearsal, restricted to the disposable validation DB.

Run with STRATEGY_TEST_DATABASE_URL pointing at a local mark_six_validation DB.
Never accepts a production database name or non-local host.
"""

import os
from pathlib import Path
import sys

from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from strategy_platform.migrations import upgrade_strategy_schema


def main():
    uri = os.environ.get("STRATEGY_TEST_DATABASE_URL", "")
    parsed = make_url(uri)
    if parsed.database != "mark_six_validation" or parsed.host != "127.0.0.1" or parsed.username != "validation":
        raise SystemExit("Only the dedicated local validation database is allowed")
    engine = create_engine(uri)
    old = MetaData()
    old.reflect(engine)
    old.drop_all(engine)
    with engine.begin() as connection:
        for sql in (
            "CREATE TABLE strategy_workspaces(id INTEGER PRIMARY KEY, slug VARCHAR(80))",
            "CREATE TABLE strategy_backup_destinations(id INTEGER PRIMARY KEY)",
            "CREATE TABLE strategy_revisions(id INTEGER PRIMARY KEY)",
            "CREATE TABLE strategy_backup_outbox(id INTEGER PRIMARY KEY, destination_id INTEGER, revision_id INTEGER, CONSTRAINT uq_strategy_backup_revision_destination UNIQUE(destination_id,revision_id), FOREIGN KEY(destination_id) REFERENCES strategy_backup_destinations(id), FOREIGN KEY(revision_id) REFERENCES strategy_revisions(id))",
            "INSERT INTO strategy_workspaces VALUES(1,'legacy')",
            "INSERT INTO strategy_backup_destinations VALUES(1)",
            "INSERT INTO strategy_revisions VALUES(1)",
            "INSERT INTO strategy_backup_outbox VALUES(1,1,1)",
        ):
            connection.execute(text(sql))
    assert upgrade_strategy_schema(engine) == [1, 2]
    assert upgrade_strategy_schema(engine) == []
    with engine.begin() as connection:
        workspace_id = connection.execute(text("SELECT public_id FROM strategy_workspaces")).scalar()
        assert len(workspace_id) == 36
        assert connection.execute(text("SELECT event_type FROM strategy_backup_outbox WHERE id=1")).scalar() == "revision"
        connection.execute(text("INSERT INTO strategy_backup_outbox(id,destination_id,revision_id,event_type) VALUES(2,1,1,'tombstone')"))
        # Simulate a MySQL implicit-DDL-commit interruption between index steps.
        connection.execute(text("DELETE FROM strategy_schema_migrations"))
        connection.execute(text("CREATE UNIQUE INDEX uq_strategy_backup_event ON strategy_backup_outbox(destination_id,revision_id,event_type)"))
        connection.execute(text("ALTER TABLE strategy_backup_outbox DROP INDEX uq_strategy_backup_revision_destination"))
    assert upgrade_strategy_schema(engine) == [1, 2]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT public_id FROM strategy_workspaces")).scalar() == workspace_id
        assert connection.execute(text("SELECT COUNT(*) FROM strategy_backup_outbox")).scalar() == 2
        indexes = inspect(connection).get_indexes("strategy_backup_outbox")
        assert not any(item["name"] == "uq_strategy_backup_event" for item in indexes)
    final = MetaData()
    final.reflect(engine)
    final.drop_all(engine)
    engine.dispose()
    print("PASS: legacy MariaDB upgrade, data preservation, event uniqueness, interrupted-DDL recovery and idempotent rerun")


if __name__ == "__main__":
    main()
