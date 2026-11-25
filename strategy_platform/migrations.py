"""Numbered, repeatable additive upgrades for the strategy platform.

Existing table creation stays with SQLAlchemy metadata; revisions here handle
columns, indexes and deterministic backfills on SQLite and MySQL/MariaDB.
The ledger is installation state, not user backup data. Failed steps can be
rerun (MySQL DDL commits implicitly), so every operation inspects first.
"""

from datetime import datetime
import uuid

from sqlalchemy import MetaData, Table, UniqueConstraint, inspect, text

VERSION = 2


def _columns(connection, table):
    return {column["name"] for column in inspect(connection).get_columns(table)}


def _upgrade_outbox_unique(connection):
    constraints = inspect(connection).get_unique_constraints("strategy_backup_outbox")
    old = next((item for item in constraints if item["name"] == "uq_strategy_backup_revision_destination"), None)
    if connection.dialect.name in {"mysql", "mariadb"}:
        temporary = any(item["name"] == "uq_strategy_backup_event" for item in constraints)
        correct = old is not None and old["column_names"] == ["destination_id", "revision_id", "event_type"]
        if not correct:
            if not temporary:
                connection.execute(text("CREATE UNIQUE INDEX uq_strategy_backup_event ON strategy_backup_outbox(destination_id,revision_id,event_type)"))
            if old is not None:
                connection.execute(text("ALTER TABLE strategy_backup_outbox DROP INDEX uq_strategy_backup_revision_destination"))
            connection.execute(text("ALTER TABLE strategy_backup_outbox ADD CONSTRAINT uq_strategy_backup_revision_destination UNIQUE(destination_id,revision_id,event_type)"))
            temporary = True
        if temporary:
            connection.execute(text("DROP INDEX uq_strategy_backup_event ON strategy_backup_outbox"))
        return
    if old is None or old["column_names"] == ["destination_id", "revision_id", "event_type"]:
        return
    original = Table("strategy_backup_outbox", MetaData(), autoload_with=connection)
    replacement = original.to_metadata(MetaData(), name="strategy_backup_outbox_v1")
    for constraint in list(replacement.constraints):
        if isinstance(constraint, UniqueConstraint) and constraint.name == "uq_strategy_backup_revision_destination":
            replacement.constraints.remove(constraint)
    replacement.append_constraint(UniqueConstraint("destination_id", "revision_id", "event_type",
                                                 name="uq_strategy_backup_revision_destination"))
    replacement.indexes.clear()
    # Referenced tables must be in metadata for SQLite FK DDL compilation.
    for foreign in replacement.foreign_keys:
        table_name = foreign.target_fullname.split(".")[0]
        if table_name not in replacement.metadata.tables:
            Table(table_name, replacement.metadata, autoload_with=connection)
    replacement.create(connection)
    names = ",".join(connection.dialect.identifier_preparer.quote(column.name) for column in original.columns)
    connection.execute(text(f"INSERT INTO strategy_backup_outbox_v1 ({names}) SELECT {names} FROM strategy_backup_outbox"))
    original.drop(connection)
    connection.execute(text("ALTER TABLE strategy_backup_outbox_v1 RENAME TO strategy_backup_outbox"))
    # Recreate original indexes only after the old table's index names disappear.
    for index in original.indexes:
        index.create(connection)


def upgrade_strategy_schema(engine):
    """Apply the platform revision once; return the applied version numbers."""
    with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            # sqlite3's legacy transaction mode does not begin on DDL alone.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.execute(text("CREATE TABLE IF NOT EXISTS strategy_schema_migrations (version INTEGER PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)"))
        versions = set(connection.execute(text("SELECT version FROM strategy_schema_migrations")).scalars())
        applied = []
        if 1 not in versions:
            tables = set(inspect(connection).get_table_names())
            if "strategy_workspaces" in tables:
                if "public_id" not in _columns(connection, "strategy_workspaces"):
                    connection.execute(text("ALTER TABLE strategy_workspaces ADD COLUMN public_id VARCHAR(36)"))
                for row_id in connection.execute(text("SELECT id FROM strategy_workspaces WHERE public_id IS NULL OR public_id='' ")).scalars():
                    connection.execute(text("UPDATE strategy_workspaces SET public_id=:public_id WHERE id=:id"),
                                       {"public_id": str(uuid.uuid4()), "id": row_id})
                indexes = inspect(connection).get_indexes("strategy_workspaces")
                uniques = inspect(connection).get_unique_constraints("strategy_workspaces")
                if not any(item.get("column_names") == ["public_id"] and item.get("unique", True) for item in indexes + uniques):
                    connection.execute(text("CREATE UNIQUE INDEX uq_strategy_workspace_public_id ON strategy_workspaces(public_id)"))
            if "strategy_backup_outbox" in tables:
                columns = _columns(connection, "strategy_backup_outbox")
                if "event_type" not in columns:
                    connection.execute(text("ALTER TABLE strategy_backup_outbox ADD COLUMN event_type VARCHAR(20) NOT NULL DEFAULT 'revision'"))
                if "payload_json" not in columns:
                    text_type = "MEDIUMTEXT" if connection.dialect.name in {"mysql", "mariadb"} else "TEXT"
                    connection.execute(text(f"ALTER TABLE strategy_backup_outbox ADD COLUMN payload_json {text_type}"))
                _upgrade_outbox_unique(connection)
            connection.execute(text("INSERT INTO strategy_schema_migrations(version,applied_at) VALUES (1,:at)"),
                               {"at": datetime.now().isoformat()})
            applied.append(1)
        if 2 not in versions:
            tables = set(inspect(connection).get_table_names())
            if "strategy_workspaces" in tables and "name" in _columns(connection, "strategy_workspaces"):
                connection.execute(text("UPDATE strategy_workspaces SET name=:name WHERE slug=:slug AND name=:old"),
                                   {"name": "策略工作区", "slug": "friends", "old": "朋友策略工作区"})
            connection.execute(text("INSERT INTO strategy_schema_migrations(version,applied_at) VALUES (2,:at)"),
                               {"at": datetime.now().isoformat()})
            applied.append(2)
        return applied
