# Intel Data Dedup Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Optimize server-side intel ingestion so repeated OCR scans, repeated channel log submissions, and repeated alert streams do not inflate stored observations, alert counts, or frontend lists.

**Architecture:** Keep clients simple: detection client uploads cleaned OCR observations and channel client uploads parsed channel observations. The server becomes the canonical deduplication boundary by deriving stable fingerprints, merging repeated observations inside a short time window, and exposing aggregate fields for the React workbench. Alert generation stays independent but consumes deduplicated observations so SSE and alert lists do not repeat the same event.

**Tech Stack:** Python, pytest, `IntelStore`, `SQLiteIntelStore`, `Observation`, `ScoringEngine`, HTTP `/api/v1/observations`, React workbench consumers.

---

## File Structure

- Modify: `app/server/intel_store.py`
  - Add canonical observation fingerprint helpers.
  - Add short-window merge logic for OCR-like observations.
  - Preserve existing exact dedupe for channel logs.
- Modify: `app/server/sqlite_store.py`
  - Continue inheriting `IntelStore.add_observation`; verify persistence of merged fields.
- Modify: `app/core/models.py`
  - Add optional observation aggregation metadata only if needed by store output.
- Modify: `app/server/http_server.py`
  - Return whether a submitted observation was created or merged.
- Modify: `frontend/src/features/workbench/types.ts`
  - Add optional aggregate fields returned by the server.
- Modify: `frontend/src/features/workbench/observations.ts`
  - Show merged observation summaries without duplicating identical rows.
- Test: `tests/test_intel_store.py`
- Test: `tests/test_sqlite_store.py`
- Test: `tests/test_http_server.py`
- Test: `frontend/src/features/workbench/observations.test.ts`
- Docs: `docs/intel-platform-architecture.md`
- Docs: `docs/intel-platform-roadmap.md`

## Data Rules

1. Channel log exact idempotency remains:
   - Duplicate key: `source + source_instance + seen_at + raw_text`.
   - Reason: a chat log line is immutable and replayable.
2. OCR short-window merge is added:
   - Applies to sources: `local_ocr`, `ocr`, `eve-sentry-detector`.
   - Merge key: `source + source_instance + system_id/system_name + sorted names/character_ids`.
   - Merge window: default 30 seconds.
   - Merge result: keep the original observation id, update `received_at`, increment `metadata.seen_count`, set `metadata.last_seen_at`, and preserve earliest `seen_at`.
3. Manual observations are not auto-merged unless they provide the same explicit id.
4. Alert generation must not create a new alert when the observation was merged into an existing observation.
5. Frontend should display one row with a count such as `x3` rather than three identical rows.

---

### Task 1: Store-Level OCR Merge Contract

**Files:**
- Modify: `tests/test_intel_store.py`
- Modify: `app/server/intel_store.py`

- [ ] **Step 1: Write failing tests for exact channel dedupe and OCR window merge**

Add this test to `tests/test_intel_store.py`:

```python
def test_add_observation_merges_repeated_ocr_within_window(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    payload = {
        "source": "eve-sentry-detector",
        "source_instance": "detector-client:test",
        "system_name": "S-KSWL",
        "names": ["Alice", "Bob"],
        "raw_text": "Alice, Bob",
        "seen_at": "2026-07-03T10:00:00+00:00",
        "received_at": "2026-07-03T10:00:00+00:00",
    }

    first = store.add_observation(payload)
    second = store.add_observation(
        {
            **payload,
            "id": "new-client-id",
            "received_at": "2026-07-03T10:00:20+00:00",
        }
    )
    distinct = store.add_observation(
        {
            **payload,
            "id": "outside-window",
            "received_at": "2026-07-03T10:00:45+00:00",
        }
    )

    observations = store.list_observations()

    assert second.observation_id == first.observation_id
    assert distinct.observation_id == "outside-window"
    assert len(observations) == 2
    assert observations[0]["metadata"]["seen_count"] == 1
    assert observations[1]["metadata"]["seen_count"] == 2
    assert observations[1]["metadata"]["last_seen_at"] == "2026-07-03T10:00:20+00:00"
```

- [ ] **Step 2: Run the failing test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_intel_store.py::test_add_observation_merges_repeated_ocr_within_window -q
```

Expected: fail because OCR observations are currently stored as separate rows when `seen_at` or id differs.

- [ ] **Step 3: Implement OCR merge helpers**

In `app/server/intel_store.py`, add constants near `CHANNEL_ADJACENT_SYSTEM_WINDOW_SECONDS`:

```python
OCR_MERGE_WINDOW_SECONDS = 30
OCR_MERGE_SOURCES = {"local_ocr", "ocr", "eve-sentry-detector"}
```

Add helper methods inside `IntelStore`:

```python
def _find_mergeable_observation(self, report: IntelReport) -> IntelReport | None:
    key = self._observation_merge_key(report)
    if key is None:
        return None
    report_time = self._parse_timestamp(report.received_at) or self._parse_timestamp(report.seen_at)
    if report_time is None:
        return None
    for existing in reversed(self._reports):
        if self._observation_merge_key(existing) != key:
            continue
        existing_time = self._parse_timestamp(existing.received_at) or self._parse_timestamp(existing.seen_at)
        if existing_time is None:
            continue
        age = abs((report_time - existing_time).total_seconds())
        if age <= OCR_MERGE_WINDOW_SECONDS:
            return existing
    return None

def _observation_merge_key(self, report: IntelReport) -> tuple[str, str, str, tuple[str, ...], tuple[int, ...]] | None:
    source = (report.source.strip() or "api").casefold()
    if source not in OCR_MERGE_SOURCES:
        return None
    source_instance = (report.source_instance.strip() or source).casefold()
    system_key = str(report.system_id or report.system).strip().casefold()
    names = tuple(sorted(name.casefold() for name in report.names))
    character_ids = tuple(sorted(report.character_ids))
    if not names and not character_ids:
        return None
    return (source, source_instance, system_key, names, character_ids)

def _merge_observation_report(self, existing: IntelReport, incoming: IntelReport) -> IntelReport:
    metadata = dict(existing.metadata)
    metadata["seen_count"] = int(metadata.get("seen_count") or 1) + 1
    metadata["last_seen_at"] = incoming.received_at or incoming.seen_at
    existing.metadata = metadata
    existing.received_at = max(existing.received_at, incoming.received_at)
    return existing
```

- [ ] **Step 4: Wire merge into `add_observation`**

Inside `add_observation`, after the exact duplicate check and before appending:

```python
merge_target = self._find_mergeable_observation(report)
if merge_target is not None:
    merged = self._merge_observation_report(merge_target, report)
    self._save_reports()
    self._alert_cache.pop(merged.report_id, None)
    return merged.to_observation()
```

- [ ] **Step 5: Run store tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_intel_store.py -q
```

Expected: all tests pass.

---

### Task 2: SQLite Persistence For Merged Metadata

**Files:**
- Modify: `tests/test_sqlite_store.py`
- Modify: `app/server/sqlite_store.py` only if the inherited save path does not persist merged metadata correctly.

- [ ] **Step 1: Write failing SQLite merge persistence test**

Add to `tests/test_sqlite_store.py`:

```python
def test_sqlite_store_persists_merged_ocr_metadata(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])
    payload = {
        "source": "eve-sentry-detector",
        "source_instance": "detector-client:test",
        "system_name": "S-KSWL",
        "names": ["Alice"],
        "raw_text": "Alice",
        "seen_at": "2026-07-03T10:00:00+00:00",
        "received_at": "2026-07-03T10:00:00+00:00",
    }

    first = store.add_observation(payload)
    second = store.add_observation(
        {
            **payload,
            "id": "second-id",
            "received_at": "2026-07-03T10:00:10+00:00",
        }
    )

    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    observations = reloaded.list_observations()

    assert second.observation_id == first.observation_id
    assert len(observations) == 1
    assert observations[0]["metadata"]["seen_count"] == 2
    assert observations[0]["metadata"]["last_seen_at"] == "2026-07-03T10:00:10+00:00"
```

- [ ] **Step 2: Run the failing test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_sqlite_store.py::test_sqlite_store_persists_merged_ocr_metadata -q
```

Expected: pass if Task 1 implementation uses `_save_reports`; fail only if SQLite row replacement misses metadata.

- [ ] **Step 3: Fix SQLite only if needed**

If the test fails, update `_row_from_report` or `_report_from_row` in `app/server/sqlite_store.py` so `metadata_json`, `received_at`, and acknowledgement fields are preserved. Do not add a SQLite-specific merge path.

- [ ] **Step 4: Run SQLite tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_sqlite_store.py -q
```

Expected: all tests pass.

---

### Task 3: HTTP Response Indicates Created vs Merged

**Files:**
- Modify: `tests/test_http_server.py`
- Modify: `app/server/http_server.py`

- [ ] **Step 1: Write failing API contract test**

Add to `tests/test_http_server.py`:

```python
def test_create_observation_reports_merged_duplicate_ocr(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        payload = {
            "source": "eve-sentry-detector",
            "source_instance": "detector-client:test",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "raw_text": "Alice",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "received_at": "2026-07-03T10:00:00+00:00",
        }
        status, first = request_json(
            f"{server.url}/api/v1/observations",
            method="POST",
            payload=payload,
        )
        status2, second = request_json(
            f"{server.url}/api/v1/observations",
            method="POST",
            payload={**payload, "id": "second-id", "received_at": "2026-07-03T10:00:10+00:00"},
        )

        assert status == 201
        assert status2 == 200
        assert first["created"] is True
        assert second["created"] is False
        assert second["merged"] is True
        assert second["observation"]["id"] == first["observation"]["id"]
    finally:
        server.stop()
```

- [ ] **Step 2: Run the failing test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_http_server.py::test_create_observation_reports_merged_duplicate_ocr -q
```

Expected: fail because current response always uses `201` and does not expose `created` or `merged`.

- [ ] **Step 3: Add store result metadata without breaking callers**

In `app/server/intel_store.py`, add an internal attribute updated by `add_observation`:

```python
self._last_add_observation_result = "created"
```

Set it to `"duplicate"` for exact duplicates, `"merged"` for OCR merges, and `"created"` for new inserts. Add:

```python
def last_add_observation_result(self) -> str:
    return getattr(self, "_last_add_observation_result", "created")
```

- [ ] **Step 4: Use result metadata in HTTP handlers**

In both legacy and v1 observation POST handlers in `app/server/http_server.py`, after `add_observation`:

```python
result = self._store().last_add_observation_result()
status = HTTPStatus.CREATED if result == "created" else HTTPStatus.OK
payload = {
    "ok": True,
    "created": result == "created",
    "merged": result == "merged",
    "duplicate": result == "duplicate",
    "observation": observation.to_dict(),
    "alert": self._alert_for_observation(observation.observation_id),
}
self._send_json(payload, status)
```

- [ ] **Step 5: Run HTTP tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_http_server.py -q
```

Expected: all tests pass; update existing tests that assert `201` for idempotent duplicate submissions to accept `200`.

---

### Task 4: Alert And SSE Dedup Validation

**Files:**
- Modify: `tests/test_http_server.py`
- Modify: `app/server/intel_store.py` only if merged observations still generate new alerts.

- [ ] **Step 1: Write failing alert count test**

Add to `tests/test_http_server.py`:

```python
def test_merged_ocr_observation_does_not_create_second_alert(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        payload = {
            "source": "eve-sentry-detector",
            "source_instance": "detector-client:test",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "raw_text": "Alice",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "received_at": "2026-07-03T10:00:00+00:00",
        }
        request_json(f"{server.url}/api/v1/observations", method="POST", payload=payload)
        request_json(
            f"{server.url}/api/v1/observations",
            method="POST",
            payload={**payload, "id": "second-id", "received_at": "2026-07-03T10:00:10+00:00"},
        )

        status, alerts = request_json(f"{server.url}/api/v1/alerts")

        assert status == 200
        assert alerts["count"] == 1
        assert alerts["alerts"][0]["source_observation_id"]
    finally:
        server.stop()
```

- [ ] **Step 2: Run the failing test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_http_server.py::test_merged_ocr_observation_does_not_create_second_alert -q
```

Expected: pass after Task 1 if alert generation only reads stored observations. If it fails, fix `_alert_cache` invalidation so merged reports do not create an additional report id.

- [ ] **Step 3: Run event stream tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_http_server.py -q
```

Expected: alert stream tests continue to pass and no duplicate alert id is emitted.

---

### Task 5: Frontend Observation Aggregation

**Files:**
- Modify: `frontend/src/features/workbench/types.ts`
- Modify: `frontend/src/features/workbench/observations.ts`
- Modify: `frontend/src/features/workbench/observations.test.ts`

- [ ] **Step 1: Write frontend failing test**

Add to `frontend/src/features/workbench/observations.test.ts`:

```typescript
it("shows merged OCR observations as one hostile pilot observation", () => {
  const payload = {
    reports: [],
    observations: [
      {
        id: "obs-1",
        source: "eve-sentry-detector",
        system_name: "S-KSWL",
        names: ["Alice"],
        raw_text: "Alice",
        seen_at: "2026-07-03T10:00:00+00:00",
        received_at: "2026-07-03T10:00:10+00:00",
        metadata: { seen_count: 2, last_seen_at: "2026-07-03T10:00:10+00:00" },
      },
    ],
  };

  const items = buildHostilePilotObservations(payload as any);

  expect(items).toHaveLength(1);
  expect(items[0].name).toBe("Alice");
  expect(items[0].sourceLabel).toContain("OCR");
  expect(items[0].repeatCount).toBe(2);
});
```

- [ ] **Step 2: Run frontend failing test**

Run:

```powershell
npm test -- observations.test.ts
```

Expected: fail because `repeatCount` is not mapped yet.

- [ ] **Step 3: Add optional fields**

In `frontend/src/features/workbench/types.ts`, add:

```typescript
metadata?: Record<string, unknown>;
```

to observation/report types if it is missing, and add `repeatCount?: number` to the hostile observation view model.

- [ ] **Step 4: Map repeat count**

In `frontend/src/features/workbench/observations.ts`, derive:

```typescript
const repeatCount = Number(item.metadata?.seen_count ?? 1);
```

Use `repeatCount > 1 ? repeatCount : undefined` on the view model.

- [ ] **Step 5: Run frontend tests**

Run:

```powershell
npm test -- observations.test.ts
```

Expected: all observation tests pass.

---

### Task 6: Documentation And Rollout

**Files:**
- Modify: `docs/intel-platform-architecture.md`
- Modify: `docs/intel-platform-roadmap.md`
- Modify: `docs/local-integration.md`

- [ ] **Step 1: Document dedupe layers**

Add this section to `docs/intel-platform-architecture.md`:

```markdown
## Intel Deduplication Model

The server is the canonical deduplication boundary. Channel log observations are idempotent by `source`, `source_instance`, `seen_at`, and `raw_text`. OCR observations are merged within a short server-side window by source, source instance, system, and target identity. Merged OCR observations retain the original observation id and update `metadata.seen_count` plus `metadata.last_seen_at`.
```

- [ ] **Step 2: Update roadmap**

In `docs/intel-platform-roadmap.md`, move “OCR short-window server-side dedupe” from TODO to completed once all tests pass.

- [ ] **Step 3: Add local integration note**

In `docs/local-integration.md`, add:

```markdown
When validating OCR deduplication, do not use invented intel samples. Use a controlled local POST payload or existing test fixtures, and verify that repeated OCR submissions within 30 seconds return `created: false`, `merged: true`.
```

- [ ] **Step 4: Run full verification**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_intel_store.py tests\test_sqlite_store.py tests\test_http_server.py -q
npm test -- observations.test.ts
npm run build
```

Expected: backend tests pass, frontend tests pass, frontend build succeeds.

---

## Acceptance Checklist

- [ ] Reposting the exact same channel line does not create a second observation.
- [ ] Reposting the same OCR names in the same system within 30 seconds merges into the existing observation.
- [ ] OCR merge updates `metadata.seen_count` and `metadata.last_seen_at`.
- [ ] OCR merge does not create a second alert or second SSE event.
- [ ] A repeated OCR submission outside the merge window creates a distinct observation.
- [ ] Manual observations are not silently merged.
- [ ] Frontend shows one merged row with repeat count rather than duplicate rows.
- [ ] SQLite persistence keeps merged metadata across restart.
