"""Real PostgreSQL relationship transaction and concurrent-writer regression tests."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from app.esi.relation_archive import RelationArchive
from tests.test_personnel_postgres import (
    postgres_dsn,  # noqa: F401 -- Also applies the opt-in DSN skip.
)
from tests.test_relation_archive import CONTEXT, source

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row


@pytest.fixture
def repository(postgres_dsn):  # noqa: F811 -- Imported pytest fixture.
    connection = lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    repository = RelationArchive(connection)
    repository.migrate()
    return repository


def test_postgres_roundtrip_and_only_one_concurrent_writer_wins(repository):
    repository.save(CONTEXT, source(), expected_revision=0)
    def write(value):
        return repository.save(CONTEXT, replace(source(value), successful_at=1100), expected_revision=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [-5, 0]))
    assert sum(result is not None for result in results) == 1
    current = repository.load(CONTEXT, "corporation", 10)
    assert current.revision == 2
    assert current.entries[("corporation", 30)] in {-5, 0}


def test_postgres_constraints_roll_back_entire_snapshot(repository):
    saved = repository.save(CONTEXT, source(), expected_revision=0)
    with repository.connection() as connection:
        connection.execute("ALTER TABLE personnel_relation_entries ADD CONSTRAINT test_no_negative CHECK (standing >= 0)")
    with pytest.raises(psycopg.errors.CheckViolation):
        repository.save(CONTEXT, source(-5), expected_revision=1)
    assert repository.load(CONTEXT, "corporation", 10) == saved
