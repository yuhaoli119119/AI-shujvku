from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection


class MigrationSchemaError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationSchema:
    name: str
    quoted_name: str

    def table(self, connection: Connection, table_name: str) -> str:
        quoted_table = connection.dialect.identifier_preparer.quote_identifier(table_name)
        return f"{self.quoted_name}.{quoted_table}"

    def identifier(self, connection: Connection, identifier: str) -> str:
        return connection.dialect.identifier_preparer.quote_identifier(identifier)


def resolve_migration_schema(
    connection: Connection,
    *,
    expected_schema: str | None = None,
    destructive: bool = False,
) -> MigrationSchema:
    """Resolve one concrete schema and reject search-path fallback for DDL/DML."""

    current = connection.scalar(text("SELECT current_schema()"))
    if not isinstance(current, str) or not current.strip():
        raise MigrationSchemaError("migration_current_schema_unavailable")
    current = current.strip()
    if destructive and expected_schema is None:
        raise MigrationSchemaError("destructive_migration_requires_expected_schema")
    if expected_schema is not None:
        expected = str(expected_schema).strip()
        if not expected or current != expected:
            raise MigrationSchemaError(
                f"migration_schema_mismatch:expected={expected or '<empty>'}:current={current}"
            )
    if current in {"information_schema", "pg_catalog"} or current.startswith("pg_"):
        raise MigrationSchemaError(f"migration_schema_not_allowed:{current}")
    exists = connection.scalar(
        text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :schema)"),
        {"schema": current},
    )
    if not exists:
        raise MigrationSchemaError(f"migration_schema_not_found:{current}")
    quoted = connection.dialect.identifier_preparer.quote_schema(current)
    return MigrationSchema(name=current, quoted_name=quoted)
