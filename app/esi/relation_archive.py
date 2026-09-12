"""Atomic per-source relationship replacement with optimistic fencing; no ESI access."""

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from app.esi.organization_relations import RelationSource, validate_source


class RelationArchive:
    def __init__(self, connection_factory, *, dialect="postgres"):
        if dialect not in {"postgres", "sqlite"}:
            raise ValueError("unsupported relation dialect")
        self.connection = connection_factory
        self.row_lock = " FOR UPDATE" if dialect == "postgres" else ""

    def migrate(self):
        schema = Path(__file__).with_name("relation_schema.sql").read_text(encoding="utf-8")
        with self.connection() as connection:
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)

    @staticmethod
    def key(context, kind, entity_id):
        if not isinstance(context, str) or len(context) != 64 or any(c not in "0123456789abcdef" for c in context):
            raise ValueError("relation context must be an opaque digest")
        validate_source(RelationSource(kind, entity_id))
        return context, kind, entity_id

    def load(self, context, kind, entity_id):
        key = self.key(context, kind, entity_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM personnel_relation_snapshots WHERE context_key=%s AND source_kind=%s AND source_id=%s"
                + self.row_lock, key).fetchone()
            if row is None:
                return RelationSource(kind, entity_id)
            entries = connection.execute(
                "SELECT target_kind,target_id,standing FROM personnel_relation_entries "
                "WHERE context_key=%s AND source_kind=%s AND source_id=%s", key).fetchall()
            source = RelationSource(kind, entity_id, row["revision"], row["successful_at"], row["expires_at"],
                                    row["retry_at"], row["failures"], bool(row["authorized"]),
                                    MappingProxyType({(item["target_kind"], item["target_id"]): item["standing"]
                                                      for item in entries}))
            validate_source(source)
            return source

    def save(self, context, source, *, expected_revision):
        """Returns None for a losing writer; rollback leaves both rows and metadata intact."""
        key = self.key(context, source.kind, source.entity_id)
        validate_source(source)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid expected revision")
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO personnel_relation_snapshots (context_key,source_kind,source_id) VALUES (%s,%s,%s) "
                "ON CONFLICT (context_key,source_kind,source_id) DO NOTHING", key)
            row = connection.execute(
                "SELECT revision,successful_at FROM personnel_relation_snapshots "
                "WHERE context_key=%s AND source_kind=%s AND source_id=%s" + self.row_lock, key).fetchone()
            if row["revision"] != expected_revision:
                return None
            if (source.successful_at is not None and row["successful_at"] is not None
                    and source.successful_at < row["successful_at"]):
                return None
            connection.execute(
                "DELETE FROM personnel_relation_entries WHERE context_key=%s AND source_kind=%s AND source_id=%s", key)
            # A bounded VALUES batch avoids one transaction/request for each relationship.
            rows = list(source.entries.items())
            for offset in range(0, len(rows), 100):
                batch = rows[offset:offset + 100]
                placeholders = ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(batch))
                parameters = tuple(value for (kind, cid), standing in batch for value in (*key, kind, cid, standing))
                connection.execute("INSERT INTO personnel_relation_entries "
                                   "(context_key,source_kind,source_id,target_kind,target_id,standing) VALUES "
                                   + placeholders, parameters)
            revision = expected_revision + 1
            connection.execute(
                "UPDATE personnel_relation_snapshots SET revision=%s,successful_at=%s,expires_at=%s,"
                "retry_at=%s,failures=%s,authorized=%s WHERE context_key=%s AND source_kind=%s AND source_id=%s",
                (revision, source.successful_at, source.expires_at, source.retry_at, source.failures,
                 int(source.authorized), *key))
        return replace(source, revision=revision)
