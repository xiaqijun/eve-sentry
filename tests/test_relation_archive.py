"""Atomic snapshot storage tests against transactional in-memory SQLite."""

import sqlite3
from contextlib import contextmanager
from dataclasses import replace

import pytest

from app.esi.organization_relations import RelationSource, organization_entries
from app.esi.relation_archive import RelationArchive
from tests.test_personnel_archive import SqliteConnection

CONTEXT = "a" * 64


@pytest.fixture
def archive():
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    @contextmanager
    def connection():
        with database:
            yield SqliteConnection(database)
    repository = RelationArchive(connection, dialect="sqlite")
    repository.migrate()
    yield repository
    database.close()


def source(value=5):
    entries = organization_entries([{"contact_type": "corporation", "contact_id": 30, "standing": value},
                                    {"contact_type": "character", "contact_id": 99, "standing": -10}])
    return RelationSource("corporation", 10, successful_at=1000, expires_at=1300, retry_at=1300, entries=entries)


def test_complete_snapshot_roundtrip_omits_individuals_and_fences_old_writer(archive):
    saved = archive.save(CONTEXT, source(), expected_revision=0)
    assert saved.revision == 1
    assert archive.load(CONTEXT, "corporation", 10) == saved
    assert len(saved.entries) == 1
    assert archive.save(CONTEXT, source(-10), expected_revision=0) is None
    assert archive.load(CONTEXT, "corporation", 10) == saved
    assert not archive.load("b" * 64, "corporation", 10).entries


def test_failure_preserves_success_timestamp_and_rows(archive):
    saved = archive.save(CONTEXT, source(), expected_revision=0)
    failed = replace(saved, failures=1, retry_at=1400, authorized=False)
    updated = archive.save(CONTEXT, failed, expected_revision=1)
    assert updated.successful_at == 1000 and updated.expires_at == 1300
    assert updated.entries == saved.entries
    assert not archive.load(CONTEXT, "corporation", 10).usable(1350)


def test_valid_empty_source_replaces_old_entries_without_changing_other_source(archive):
    saved = archive.save(CONTEXT, source(), expected_revision=0)
    alliance = archive.save(CONTEXT, replace(source(-5), kind="alliance", entity_id=20), expected_revision=0)
    empty = replace(saved, successful_at=1200, entries=organization_entries([]))
    archive.save(CONTEXT, empty, expected_revision=1)
    assert not archive.load(CONTEXT, "corporation", 10).entries
    assert archive.load(CONTEXT, "alliance", 20) == alliance


def test_late_timestamp_and_invalid_standing_do_not_destroy_old_data(archive):
    saved = archive.save(CONTEXT, source(), expected_revision=0)
    assert archive.save(CONTEXT, replace(source(), successful_at=900), expected_revision=1) is None
    with pytest.raises(ValueError):
        archive.save(CONTEXT, replace(source(), entries={("character", 99): 5}), expected_revision=1)
    assert archive.load(CONTEXT, "corporation", 10) == saved


def test_insert_failure_rolls_back_deleted_rows_and_metadata(archive):
    saved = archive.save(CONTEXT, source(), expected_revision=0)
    original = archive.connection
    @contextmanager
    def failing():
        with original() as connection:
            class FailInsert:
                def execute(self, query, params=()):
                    if query.startswith("INSERT INTO personnel_relation_entries"):
                        raise RuntimeError("injected database failure")
                    return connection.execute(query, params)
            yield FailInsert()
    archive.connection = failing
    with pytest.raises(RuntimeError):
        archive.save(CONTEXT, source(-5), expected_revision=1)
    archive.connection = original
    assert archive.load(CONTEXT, "corporation", 10) == saved
