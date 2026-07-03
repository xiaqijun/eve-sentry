# Active Intel State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a realtime `active_intel` state layer so OCR and intel-channel threats can disappear from current UI state while historical observations remain available.

**Architecture:** Keep `observations` as the historical audit trail and add a separate realtime state model. OCR clients upload only the detected pilot-name list plus minimal context, and the server diffs those names against active state with a 6-second grace period. Channel reports create TTL-based active state and clear messages deactivate matching realtime state without deleting history.

**Tech Stack:** Python, pytest, `IntelStore`, `SQLiteIntelStore`, HTTP `/api/v1`, React/Vite/TypeScript workbench.

---

## File Structure

- Create: `app/core/active_intel.py`
  - Defines `ActiveIntelItem`, `ActiveIntelSnapshotResult`, source constants, clear-word matching, and timestamp helpers.
- Modify: `app/server/intel_store.py`
  - Keeps in-memory `_active_intel`.
  - Adds OCR snapshot ingestion.
  - Adds channel TTL/clear state updates.
  - Exposes active intel list and expiry sweep.
- Modify: `app/server/sqlite_store.py`
  - Persists active intel rows across restarts.
- Modify: `app/server/http_server.py`
  - Adds `GET /api/v1/active-intel`.
  - Adds `POST /api/v1/ocr/snapshot`.
  - Updates `POST /api/v1/channel-lines` to refresh/clear active channel state.
  - Adds active intel to bootstrap.
- Modify: `app/intel_client.py`
  - Adds detector-client method for OCR snapshot upload.
- Modify: `app/ui/main_window.py`
  - Publishes detected OCR names instead of only newly detected threats.
- Modify: `app/engine/worker.py`
  - Emits only cleaned OCR names per scan to the UI layer.
- Modify: `frontend/src/features/workbench/types.ts`
  - Adds `ActiveIntelItem`.
- Modify: `frontend/src/features/workbench/api.ts`
  - Fetches active intel through bootstrap and optional endpoint.
- Modify: `frontend/src/features/workbench/observations.ts`
  - Builds enemy pilot observation list from `active_intel`.
- Modify: `frontend/src/features/workbench/WorkbenchPage.tsx`
  - Displays realtime active items separately from historical observations.
- Test: `tests/test_active_intel.py`
- Test: `tests/test_intel_store.py`
- Test: `tests/test_sqlite_store.py`
- Test: `tests/test_http_server.py`
- Test: `tests/test_intel_client.py`
- Test: `tests/test_worker.py`
- Test: `frontend/src/features/workbench/observations.test.ts`

---

### Task 1: Active Intel Core Model

**Files:**
- Create: `app/core/active_intel.py`
- Test: `tests/test_active_intel.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_active_intel.py`:

```python
from app.core.active_intel import (
    ActiveIntelItem,
    channel_ttl_seconds,
    contains_clear_signal,
)


def test_active_intel_item_serializes_realtime_fields():
    item = ActiveIntelItem(
        active_id="ocr:client:s-kswl:alice",
        source="eve-sentry-detector",
        source_instance="client",
        system_name="S-KSWL",
        target_type="character",
        name="Alice",
        first_seen_at="2026-07-03T10:00:00+00:00",
        last_seen_at="2026-07-03T10:00:02+00:00",
        active=True,
        seen_count=2,
        source_observation_ids=["obs-1"],
    )

    assert item.to_dict()["id"] == "ocr:client:s-kswl:alice"
    assert item.to_dict()["active"] is True
    assert item.to_dict()["seen_count"] == 2
    assert item.to_dict()["source_observation_ids"] == ["obs-1"]


def test_contains_clear_signal_matches_english_and_chinese_words():
    assert contains_clear_signal("Tama clr")
    assert contains_clear_signal("Oijanen clear")
    assert contains_clear_signal("S-KSWL 清了")
    assert contains_clear_signal("本地安全")
    assert not contains_clear_signal("Tama +3 reds")


def test_channel_ttl_seconds_uses_expected_defaults():
    assert channel_ttl_seconds({"hostile_count": 3}) == 180
    assert channel_ttl_seconds({"jump_count": 1}) == 300
    assert channel_ttl_seconds({"fleet": True}) == 900
    assert channel_ttl_seconds({"bridge": True}) == 1200
```

- [ ] **Step 2: Run the failing test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_active_intel.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'app.core.active_intel'`.

- [ ] **Step 3: Implement the core model**

Create `app/core/active_intel.py`:

```python
"""Realtime active intel state derived from historical observations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


OCR_SOURCES = {"local_ocr", "ocr", "eve-sentry-detector"}
DEFAULT_OCR_GRACE_SECONDS = 6

CLEAR_WORDS = (
    "clr",
    "clear",
    "clean",
    "安全",
    "清了",
    "已清",
    "走了",
    "没了",
    "散了",
)


@dataclass
class ActiveIntelItem:
    active_id: str
    source: str
    source_instance: str
    system_name: str
    target_type: str = "character"
    name: str = ""
    system_id: int | None = None
    character_id: int | None = None
    raw_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    first_seen_at: str = ""
    last_seen_at: str = ""
    expires_at: str = ""
    left_at: str = ""
    cleared_at: str = ""
    active: bool = True
    seen_count: int = 1
    confidence: float | None = None
    source_observation_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.active_id,
            "source": self.source,
            "source_instance": self.source_instance,
            "system_name": self.system_name,
            "system_id": self.system_id,
            "target_type": self.target_type,
            "name": self.name,
            "character_id": self.character_id,
            "raw_text": self.raw_text,
            "metadata": dict(self.metadata),
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "expires_at": self.expires_at,
            "left_at": self.left_at,
            "cleared_at": self.cleared_at,
            "active": self.active,
            "seen_count": self.seen_count,
            "confidence": self.confidence,
            "source_observation_ids": list(self.source_observation_ids),
        }


@dataclass
class ActiveIntelSnapshotResult:
    created: int = 0
    refreshed: int = 0
    missing: int = 0
    expired: int = 0
    active: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "created": self.created,
            "refreshed": self.refreshed,
            "missing": self.missing,
            "expired": self.expired,
            "active": list(self.active),
        }


def contains_clear_signal(text: str) -> bool:
    haystack = str(text or "").casefold()
    for word in CLEAR_WORDS:
        token = word.casefold()
        if re.fullmatch(r"[a-z]+", token):
            if re.search(rf"\b{re.escape(token)}\b", haystack):
                return True
        elif token in haystack:
            return True
    return False


def channel_ttl_seconds(metadata: dict[str, Any]) -> int:
    if _truthy(metadata.get("bridge")) or _truthy(metadata.get("staging")):
        return 1200
    if _truthy(metadata.get("fleet")) or _truthy(metadata.get("camp")):
        return 900
    if _positive_int(metadata.get("jump_count")) is not None:
        return 300
    if _positive_int(metadata.get("hostile_count")) is not None:
        return 180
    return 600


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
```

- [ ] **Step 4: Run the test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_active_intel.py -q
```

Expected: `3 passed`.

---

### Task 2: IntelStore OCR Snapshot State

**Files:**
- Modify: `app/server/intel_store.py`
- Test: `tests/test_intel_store.py`

- [ ] **Step 1: Write failing OCR snapshot tests**

Add to `tests/test_intel_store.py`:

```python
def test_record_ocr_snapshot_creates_and_refreshes_active_intel(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])

    first = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice", "Bob"],
        }
    )
    second = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:02+00:00",
            "names": ["Alice", "Bob"],
        }
    )

    active = store.list_active_intel(source="eve-sentry-detector")

    assert first["created"] == 2
    assert second["refreshed"] == 2
    assert len(active) == 2
    assert {item["name"] for item in active} == {"Alice", "Bob"}
    assert all(item["seen_count"] == 2 for item in active)
    assert len(store.list_observations()) == 2


def test_record_ocr_snapshot_expires_missing_names_after_grace_period(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    payload = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Hajimi6",
        "system_name": "S-KSWL",
        "names": ["Alice"],
    }

    store.record_ocr_snapshot({**payload, "seen_at": "2026-07-03T10:00:00+00:00"})
    still_active = store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:04+00:00", "names": []}
    )
    expired = store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:08+00:00", "names": []}
    )

    assert still_active["missing"] == 1
    assert still_active["expired"] == 0
    assert expired["expired"] == 1
    assert store.list_active_intel() == []
    assert store.list_active_intel(active=False)[0]["left_at"] == "2026-07-03T10:00:08+00:00"
```

- [ ] **Step 2: Run the failing tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_intel_store.py::test_record_ocr_snapshot_creates_and_refreshes_active_intel tests\test_intel_store.py::test_record_ocr_snapshot_expires_missing_names_after_grace_period -q
```

Expected: fail because `record_ocr_snapshot` and `list_active_intel` do not exist.

- [ ] **Step 3: Add in-memory active state**

In `app/server/intel_store.py`, import:

```python
from app.core.active_intel import (
    ActiveIntelItem,
    ActiveIntelSnapshotResult,
    DEFAULT_OCR_GRACE_SECONDS,
)
```

In `IntelStore.__init__`, add before `_load_reports()`:

```python
self._active_intel: dict[str, ActiveIntelItem] = {}
```

- [ ] **Step 4: Implement active id and timestamp helpers**

Add methods to `IntelStore`:

```python
def _active_ocr_id(self, client_id: str, system: str, name: str) -> str:
    raw = "|".join(
        [
            "ocr",
            client_id.strip().casefold(),
            system.strip().casefold(),
            name.strip().casefold(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _seconds_between_iso(self, left: str, right: str) -> float | None:
    left_dt = self._parse_timestamp(left)
    right_dt = self._parse_timestamp(right)
    if left_dt is None or right_dt is None:
        return None
    return (right_dt - left_dt).total_seconds()
```

- [ ] **Step 5: Implement `record_ocr_snapshot`**

Add to `IntelStore`:

```python
def record_ocr_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
    client_id = str(payload.get("client_id") or "").strip()
    if not client_id:
        raise ValueError("client_id is required")
    source_instance = str(payload.get("source_instance") or client_id).strip()
    system_name = self._normalize_system(str(payload.get("system_name") or "Unknown"))
    system_id = self._optional_int(payload.get("system_id"))
    names = self._normalize_names(payload.get("names"))
    seen_at = str(payload.get("seen_at") or utc_now_iso())
    raw_text = ", ".join(names)
    confidence = payload.get("confidence")

    result = ActiveIntelSnapshotResult()
    visible_ids: set[str] = set()

    with self._lock:
        self._ensure_system(system_name)
        for name in names:
            active_id = self._active_ocr_id(client_id, source_instance, system_name, name)
            visible_ids.add(active_id)
            item = self._active_intel.get(active_id)
            if item is None or not item.active:
                observation = self.add_observation(
                    {
                        "source": "eve-sentry-detector",
                        "source_instance": source_instance,
                        "system_name": system_name,
                        "system_id": system_id,
                        "names": [name],
                        "raw_text": name,
                        "confidence": confidence,
                        "seen_at": seen_at,
                        "metadata": {"client_id": client_id},
                    }
                )
                item = ActiveIntelItem(
                    active_id=active_id,
                    source="eve-sentry-detector",
                    source_instance=source_instance,
                    system_name=system_name,
                    system_id=system_id,
                    name=name,
                    raw_text=raw_text,
                    first_seen_at=seen_at,
                    last_seen_at=seen_at,
                    active=True,
                    confidence=confidence,
                    metadata={"client_id": client_id},
                    source_observation_ids=[observation.observation_id],
                )
                self._active_intel[active_id] = item
                result.created += 1
            else:
                item.last_seen_at = seen_at
                item.raw_text = raw_text
                item.active = True
                item.left_at = ""
                item.seen_count += 1
                result.refreshed += 1

        for item in self._active_intel.values():
            if item.source != "eve-sentry-detector":
                continue
            if item.metadata.get("client_id") != client_id:
                continue
            if item.system_name.casefold() != system_name.casefold():
                continue
            if item.active_id in visible_ids or not item.active:
                continue
            elapsed = self._seconds_between_iso(item.last_seen_at, seen_at)
            if elapsed is not None and elapsed > DEFAULT_OCR_GRACE_SECONDS:
                item.active = False
                item.left_at = seen_at
                result.expired += 1
            else:
                result.missing += 1

        result.active = self.list_active_intel(source="eve-sentry-detector")
    return result.to_dict()
```

- [ ] **Step 6: Implement `list_active_intel`**

Add to `IntelStore`:

```python
def list_active_intel(
    self,
    source: str = "",
    system: str = "",
    active: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    source_query = source.strip().casefold()
    system_query = system.strip().casefold()
    with self._lock:
        items = []
        for item in self._active_intel.values():
            if item.active is not active:
                continue
            if source_query and item.source.casefold() != source_query:
                continue
            if system_query and item.system_name.casefold() != system_query:
                continue
            items.append(item.to_dict())
    items.sort(key=lambda value: str(value.get("last_seen_at") or ""), reverse=True)
    if limit is not None:
        items = items[:max(0, limit)]
    return items
```

- [ ] **Step 7: Run store tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_active_intel.py tests\test_intel_store.py -q
```

Expected: all tests pass.

---

### Task 3: Channel TTL And Clear Active State

**Files:**
- Modify: `app/server/intel_store.py`
- Test: `tests/test_intel_store.py`

- [ ] **Step 1: Write failing channel tests**

Add to `tests/test_intel_store.py`:

```python
def test_channel_observation_creates_ttl_active_intel(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])

    observation = store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "S-KSWL",
            "raw_text": "Scout: S-KSWL +3 reds",
            "metadata": {"hostile_count": 3, "sender": "Scout"},
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )

    active = store.list_active_intel(source="intel_channel")

    assert len(active) == 1
    assert active[0]["system_name"] == "S-KSWL"
    assert active[0]["expires_at"] == "2026-07-03T10:03:00+00:00"
    assert active[0]["source_observation_ids"] == [observation.observation_id]


def test_channel_clear_deactivates_matching_system_state(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "S-KSWL",
            "raw_text": "Scout: S-KSWL +3 reds",
            "metadata": {"hostile_count": 3, "sender": "Scout"},
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "S-KSWL",
            "raw_text": "Scout: S-KSWL clr",
            "seen_at": "2026-07-03T10:01:00+00:00",
        }
    )

    assert store.list_active_intel(source="intel_channel") == []
    inactive = store.list_active_intel(source="intel_channel", active=False)
    assert inactive[0]["cleared_at"] == "2026-07-03T10:01:00+00:00"
```

- [ ] **Step 2: Run the failing tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_intel_store.py::test_channel_observation_creates_ttl_active_intel tests\test_intel_store.py::test_channel_clear_deactivates_matching_system_state -q
```

Expected: fail because channel observations do not update active state yet.

- [ ] **Step 3: Import channel helpers**

In `app/server/intel_store.py`, extend the import from `app.core.active_intel`:

```python
from app.core.active_intel import (
    ActiveIntelItem,
    ActiveIntelSnapshotResult,
    DEFAULT_OCR_GRACE_SECONDS,
    channel_ttl_seconds,
    contains_clear_signal,
)
```

- [ ] **Step 4: Add ISO timestamp addition helper**

Add to `IntelStore`:

```python
def _add_seconds_iso(self, timestamp: str, seconds: int) -> str:
    parsed = self._parse_timestamp(timestamp)
    if parsed is None:
        parsed = datetime.now(timezone.utc).replace(microsecond=0)
    return (parsed + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()
```

Update datetime import:

```python
from datetime import datetime, timedelta, timezone
```

- [ ] **Step 5: Add channel active id helper**

Add to `IntelStore`:

```python
def _active_channel_id(self, source_instance: str, system: str, raw_text: str) -> str:
    raw = "|".join(
        [
            "channel",
            source_instance.strip().casefold(),
            system.strip().casefold(),
            raw_text.strip().casefold(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 6: Update `add_observation` after append or duplicate return**

After a channel observation is stored or returned as duplicate, call:

```python
self._apply_channel_active_state(report)
```

Add method:

```python
def _apply_channel_active_state(self, report: IntelReport) -> None:
    if report.source.strip().casefold() != "intel_channel":
        return
    seen_at = report.seen_at or report.received_at or utc_now_iso()
    if contains_clear_signal(report.raw_text):
        self._clear_channel_active_state(report, seen_at)
        return

    active_id = self._active_channel_id(report.source_instance or report.source, report.system, report.raw_text)
    expires_at = self._add_seconds_iso(seen_at, channel_ttl_seconds(report.metadata))
    item = self._active_intel.get(active_id)
    if item is None:
        item = ActiveIntelItem(
            active_id=active_id,
            source="intel_channel",
            source_instance=report.source_instance or report.source,
            system_name=report.system,
            system_id=report.system_id,
            target_type="system",
            raw_text=report.raw_text,
            metadata=dict(report.metadata),
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            expires_at=expires_at,
            active=True,
            source_observation_ids=[report.report_id],
        )
        self._active_intel[active_id] = item
        return
    item.last_seen_at = seen_at
    item.expires_at = expires_at
    item.active = True
    item.seen_count += 1
    if report.report_id not in item.source_observation_ids:
        item.source_observation_ids.append(report.report_id)

def _clear_channel_active_state(self, report: IntelReport, cleared_at: str) -> None:
    for item in self._active_intel.values():
        if item.source != "intel_channel":
            continue
        if item.source_instance.casefold() != (report.source_instance or report.source).casefold():
            continue
        if item.system_name.casefold() != report.system.casefold():
            continue
        if not item.active:
            continue
        item.active = False
        item.cleared_at = cleared_at
```

- [ ] **Step 7: Run store tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_active_intel.py tests\test_intel_store.py -q
```

Expected: all tests pass.

---

### Task 4: HTTP API For Active Intel And OCR Snapshots

**Files:**
- Modify: `app/server/http_server.py`
- Test: `tests/test_http_server.py`

- [ ] **Step 1: Write failing HTTP tests**

Add to `tests/test_http_server.py`:

```python
def test_v1_ocr_snapshot_endpoint_updates_active_intel(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, result = request_json(
            f"{server.url}/api/v1/ocr/snapshot",
            method="POST",
            payload={
                "client_id": "detector-client:test",
                "source_instance": "EVE - Hajimi6",
                "system_name": "S-KSWL",
                "seen_at": "2026-07-03T10:00:00+00:00",
                "names": ["Alice"],
            },
        )
        status2, active = request_json(f"{server.url}/api/v1/active-intel")

        assert status == 201
        assert result["created"] == 1
        assert status2 == 200
        assert active["count"] == 1
        assert active["active_intel"][0]["name"] == "Alice"
    finally:
        server.stop()


def test_v1_bootstrap_includes_active_intel(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
        }
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, bootstrap = request_json(f"{server.url}/api/v1/bootstrap")

        assert status == 200
        assert bootstrap["active_intel"][0]["name"] == "Alice"
    finally:
        server.stop()
```

- [ ] **Step 2: Run failing tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_http_server.py::test_v1_ocr_snapshot_endpoint_updates_active_intel tests\test_http_server.py::test_v1_bootstrap_includes_active_intel -q
```

Expected: fail because endpoints and bootstrap field are missing.

- [ ] **Step 3: Add v1 GET endpoint**

In `_handle_v1_get`, add:

```python
if path == f"{API_V1_PREFIX}/active-intel":
    self._send_active_intel(parsed.query)
    return
```

Add:

```python
def _send_active_intel(self, raw_query: str = "") -> None:
    query = parse_qs(raw_query)
    try:
        limit = self._parse_optional_int(query.get("limit", [""])[0])
    except ValueError as exc:
        self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    active = self._store().list_active_intel(
        source=query.get("source", [""])[0],
        system=query.get("system", [""])[0],
        active=True,
        limit=limit,
    )
    self._send_json(
        {
            "active_intel": active,
            "count": len(active),
            "generated_at": utc_now_iso(),
        }
    )
```

- [ ] **Step 4: Add v1 POST endpoint**

In `_handle_v1_post`, add:

```python
if path == f"{API_V1_PREFIX}/ocr/snapshot":
    try:
        result = self._store().record_ocr_snapshot(self._read_json())
    except (ValueError, json.JSONDecodeError) as exc:
        self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    status = HTTPStatus.CREATED if result.get("created") else HTTPStatus.OK
    self._send_json(result, status)
    return
```

- [ ] **Step 5: Add active intel to bootstrap**

In `_bootstrap_payload`, add:

```python
"active_intel": self._store().list_active_intel(),
```

- [ ] **Step 6: Run HTTP tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_http_server.py -q
```

Expected: all tests pass.

---

### Task 5: SQLite Active Intel Persistence

**Files:**
- Modify: `app/server/sqlite_store.py`
- Test: `tests/test_sqlite_store.py`

- [ ] **Step 1: Write failing persistence test**

Add to `tests/test_sqlite_store.py`:

```python
def test_sqlite_store_persists_active_intel(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
        }
    )

    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    active = reloaded.list_active_intel()

    assert len(active) == 1
    assert active[0]["name"] == "Alice"
    assert active[0]["active"] is True
```

- [ ] **Step 2: Run failing test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_sqlite_store.py::test_sqlite_store_persists_active_intel -q
```

Expected: fail because `_active_intel` is in memory only.

- [ ] **Step 3: Create SQLite active table**

In `_migrate`, add the table from the design spec:

```python
connection.execute(
    """
    CREATE TABLE IF NOT EXISTS active_intel (
        active_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_instance TEXT NOT NULL,
        system TEXT NOT NULL,
        system_id INTEGER,
        target_type TEXT NOT NULL,
        name TEXT NOT NULL,
        character_id INTEGER,
        raw_text TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL DEFAULT '',
        left_at TEXT NOT NULL DEFAULT '',
        cleared_at TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        seen_count INTEGER NOT NULL DEFAULT 1,
        confidence REAL,
        source_observation_ids_json TEXT NOT NULL DEFAULT '[]'
    )
    """
)
```

- [ ] **Step 4: Load active rows during initialization**

After `super().__init__` in `SQLiteIntelStore.__init__`, add:

```python
self._active_intel = self._read_active_intel()
```

Implement `_read_active_intel`, `_active_item_from_row`, and `_active_row`.

- [ ] **Step 5: Persist active rows after mutations**

Override mutating methods:

```python
def record_ocr_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
    result = super().record_ocr_snapshot(payload)
    self._replace_active_intel()
    return result

def add_observation(self, observation):
    result = super().add_observation(observation)
    self._replace_active_intel()
    return result
```

- [ ] **Step 6: Run SQLite tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_sqlite_store.py -q
```

Expected: all tests pass.

---

### Task 6: Client Detected-Name Upload

**Files:**
- Modify: `app/intel_client.py`
- Modify: `app/engine/worker.py`
- Modify: `app/ui/main_window.py`
- Test: `tests/test_intel_client.py`
- Test: `tests/test_worker.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write failing client API test**

Add to `tests/test_intel_client.py`:

```python
def test_intel_api_client_posts_ocr_snapshot(tmp_path):
    with run_test_server(tmp_path) as server:
        api = IntelApiClient(server.url, timeout=1)

        result = api.post_ocr_snapshot(
            client_id="detector-client:test",
            source_instance="EVE - Hajimi6",
            system_name="S-KSWL",
            names=["Alice"],
            seen_at="2026-07-03T10:00:00+00:00",
        )

        assert result["created"] == 1
        assert api.get_active_intel()["count"] == 1
```

- [ ] **Step 2: Run failing test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_intel_client.py::test_intel_api_client_posts_ocr_snapshot -q
```

Expected: fail because client methods are missing.

- [ ] **Step 3: Implement client methods**

In `app/intel_client.py`, add:

```python
def post_ocr_snapshot(
    self,
    client_id: str,
    source_instance: str,
    system_name: str,
    names: list[str],
    seen_at: str = "",
    system_id: int | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    return self._request(
        "POST",
        "/api/v1/ocr/snapshot",
        {
            "client_id": client_id,
            "source_instance": source_instance,
            "system_name": system_name,
            "system_id": system_id,
            "names": names,
            "seen_at": seen_at,
            "confidence": confidence,
        },
    )

def get_active_intel(self, **params: Any) -> dict[str, Any]:
    return self._request("GET", self._query_path("/api/v1/active-intel", params))
```

- [ ] **Step 4: Emit only cleaned names from worker**

In `app/engine/worker.py`, add signal:

```python
ocr_snapshot = pyqtSignal(list)
```

After OCR results are parsed and cleaned, emit:

```python
self.ocr_snapshot.emit(ocr_candidate_names(ocr_results))
```

- [ ] **Step 5: Upload snapshot from main window**

In `app/ui/main_window.py`, connect:

```python
self._worker.ocr_snapshot.connect(self._publish_ocr_snapshot)
```

Add:

```python
def _publish_ocr_snapshot(self, names: list[str]) -> None:
    if self._intel_client is None:
        return
    self._refresh_intel_location()
    self._intel_client.post_ocr_snapshot(
        client_id=self._heartbeat_client_id,
        source_instance=self._window_combo.currentText(),
        system_name=self._intel_system or "Unknown",
        system_id=self._intel_system_id,
        names=names,
    )
```

- [ ] **Step 6: Run client tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_intel_client.py tests\test_worker.py tests\test_main_window.py -q
```

Expected: all tests pass.

---

### Task 7: Frontend Active Intel List

**Files:**
- Modify: `frontend/src/features/workbench/types.ts`
- Modify: `frontend/src/features/workbench/observations.ts`
- Modify: `frontend/src/features/workbench/WorkbenchPage.tsx`
- Test: `frontend/src/features/workbench/observations.test.ts`

- [ ] **Step 1: Write failing frontend test**

Add to `frontend/src/features/workbench/observations.test.ts`:

```typescript
it("uses active intel for the realtime hostile pilot list", () => {
  const payload = {
    reports: [],
    observations: [
      {
        id: "history-1",
        source: "eve-sentry-detector",
        system_name: "S-KSWL",
        names: ["Old Pilot"],
        seen_at: "2026-07-03T09:00:00+00:00",
      },
    ],
    active_intel: [
      {
        id: "active-1",
        source: "eve-sentry-detector",
        source_instance: "EVE - Hajimi6",
        system_name: "S-KSWL",
        target_type: "character",
        name: "Alice",
        active: true,
        seen_count: 3,
        last_seen_at: "2026-07-03T10:00:04+00:00",
      },
    ],
  };

  const items = buildHostilePilotObservations(payload as any);

  expect(items).toHaveLength(1);
  expect(items[0].name).toBe("Alice");
  expect(items[0].repeatCount).toBe(3);
});
```

- [ ] **Step 2: Run failing frontend test**

Run:

```powershell
npm test -- observations.test.ts
```

Expected: fail because `active_intel` is not mapped.

- [ ] **Step 3: Add frontend type**

In `frontend/src/features/workbench/types.ts`:

```typescript
export interface ActiveIntelItem {
  id: string;
  source: string;
  source_instance?: string;
  system_name: string;
  system_id?: number | null;
  target_type?: string;
  name?: string;
  character_id?: number | null;
  raw_text?: string;
  metadata?: Record<string, unknown>;
  first_seen_at?: string;
  last_seen_at?: string;
  expires_at?: string;
  left_at?: string;
  cleared_at?: string;
  active?: boolean;
  seen_count?: number;
  confidence?: number | null;
  source_observation_ids?: string[];
}
```

Add `active_intel: ActiveIntelItem[]` to `BootstrapPayload`.

- [ ] **Step 4: Map active intel in observations builder**

In `frontend/src/features/workbench/observations.ts`, prefer `payload.active_intel` when present:

```typescript
const activeIntel = payload.active_intel ?? [];
if (activeIntel.length > 0) {
  return activeIntel
    .filter((item) => item.active !== false)
    .map((item) => ({
      id: item.id,
      name: item.name || item.raw_text || "Unknown",
      systemName: item.system_name,
      source: item.source,
      sourceLabel: sourceLabel(item.source),
      lastSeenAt: item.last_seen_at || item.first_seen_at || "",
      repeatCount: item.seen_count && item.seen_count > 1 ? item.seen_count : undefined,
    }));
}
```

- [ ] **Step 5: Run frontend tests**

Run:

```powershell
npm test -- observations.test.ts
npm run build
```

Expected: tests pass and build succeeds.

---

### Task 8: Documentation And Verification

**Files:**
- Modify: `docs/intel-platform-architecture.md`
- Modify: `docs/local-integration.md`
- Modify: `docs/intel-platform-roadmap.md`

- [ ] **Step 1: Document active intel architecture**

Add to `docs/intel-platform-architecture.md`:

```markdown
## Active Intel State

Historical observations are retained as the audit trail. Realtime panels use `active_intel`, a derived state layer that can expire or clear without deleting historical observations. OCR clients upload only detected pilot-name lists; the server refreshes existing active rows, creates rows for newly visible pilots, and marks missing pilots inactive after the configured grace period. Intel-channel state uses TTL rules and clear messages to leave realtime state.
```

- [ ] **Step 2: Document local validation**

Add to `docs/local-integration.md`:

```markdown
### Active Intel Validation

Use controlled local requests rather than invented live intel. Post an OCR snapshot to `/api/v1/ocr/snapshot`, post the same snapshot again within 6 seconds, and verify `/api/v1/active-intel` still returns one row per pilot with an increased `seen_count`. Post an empty snapshot after the grace period and verify the row disappears from the default active list while historical observations remain available.
```

- [ ] **Step 3: Update roadmap**

In `docs/intel-platform-roadmap.md`, add an item under the current milestone:

```markdown
- Active Intel realtime state: OCR snapshot diffing, channel TTL, clear-message deactivation, and frontend active list.
```

- [ ] **Step 4: Run backend verification**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_active_intel.py tests\test_intel_store.py tests\test_sqlite_store.py tests\test_http_server.py tests\test_intel_client.py tests\test_worker.py tests\test_main_window.py -q
```

Expected: all selected backend tests pass.

- [ ] **Step 5: Run frontend verification**

Run:

```powershell
npm test -- observations.test.ts
npm run build
```

Expected: frontend tests pass and build succeeds.

- [ ] **Step 6: Run GitNexus change detection before commit**

Run:

```powershell
npx gitnexus analyze
```

Then run GitNexus detect changes:

```text
mcp__gitnexus.detect_changes({ repo: "eve-sentry", scope: "all" })
```

Expected: affected scope matches active intel, server API, client upload, frontend observations, and docs.

---

## Acceptance Checklist

- [ ] Historical observations remain available after realtime active intel expires.
- [ ] OCR snapshot creates active rows for newly visible pilots.
- [ ] OCR snapshot refreshes existing active rows and increments `seen_count`.
- [ ] OCR missing pilots stay active during the 6-second grace period.
- [ ] OCR missing pilots become inactive after the grace period.
- [ ] Channel observations create active rows with TTL.
- [ ] Channel clear messages deactivate matching system/channel active rows.
- [ ] Clear messages without a usable system do not globally clear active state.
- [ ] `/api/v1/active-intel` returns only active rows by default.
- [ ] `/api/v1/bootstrap` includes `active_intel`.
- [ ] Detector client uploads only detected OCR names plus minimal context.
- [ ] Frontend realtime observation list uses `active_intel` instead of raw historical observations.
- [ ] Alerts do not repeat on every OCR refresh.
- [ ] SQLite keeps active intel across server restarts.
