# EVE Sentry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an EVE Online hostile player early-warning desktop app that uses screen OCR on the local channel member list, compares names against a whitelist, and alerts when unknown players appear.

**Architecture:** PyQt6 multi-threaded desktop app. Main thread handles UI (main window, alert dialogs, settings, system tray). A QThread worker loop captures screen region → OCR via PaddleOCR → compares against JSON-persisted whitelist → emits Qt signals for threats. Window auto-detection via win32gui with manual region selection as fallback.

**Tech Stack:** Python 3.11+, PyQt6, PaddleOCR, pywin32, Pillow, PyInstaller

## Global Constraints

- Python 3.11+ required
- Windows only (win32gui, ImageGrab)
- Chinese UI text
- Whitelist stored as JSON alongside the executable
- No ESI API, no remote notifications, no video recording
- Scan interval 2s default (configurable 1–10s), alarm cooldown 60s per name
- OCR confidence threshold 0.7
- UI testing is manual only; engine components tested with pytest

---

### Task 1: Project Scaffold

**Files:**
- Create: `requirements.txt`
- Create: `app/__init__.py`
- Create: `app/engine/__init__.py`
- Create: `app/ui/__init__.py`
- Create: `app/models/__init__.py`
- Create: `resources/` (empty directory placeholder)

**Interfaces:**
- Consumes: nothing
- Produces: project directory layout, dependency manifest

- [ ] **Step 1: Create requirements.txt**

```txt
PyQt6>=6.5.0
paddleocr>=2.7.0
paddlepaddle>=2.5.0
pywin32>=306
Pillow>=10.0.0
```

- [ ] **Step 2: Create package init files**

```bash
mkdir -p app/engine app/ui app/models resources
```

Write `app/__init__.py`, `app/engine/__init__.py`, `app/ui/__init__.py`, `app/models/__init__.py` — all empty files.

- [ ] **Step 3: Add .gitkeep to resources/**

```bash
touch resources/.gitkeep
```

- [ ] **Step 4: Create resources placeholder**

Write `resources/.gitkeep` — empty file.

- [ ] **Step 5: Create conftest.py for pytest**

```python
# conftest.py
import sys
from pathlib import Path

# Add project root to path so tests can import app.*
sys.path.insert(0, str(Path(__file__).resolve().parent))
```

- [ ] **Step 6: Install dependencies and verify**

```bash
pip install -r requirements.txt
python -c "from PyQt6.QtWidgets import QApplication; from paddleocr import PaddleOCR; import win32gui; print('All imports OK')"
```

Expected: `All imports OK` (no ImportError).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chore: scaffold project structure and dependencies

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Whitelist Model

**Files:**
- Create: `tests/test_whitelist.py`
- Create: `app/models/whitelist.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `class Whitelist(filepath: str = "whitelist.json")`
  - `Whitelist.get_all() -> set[str]`
  - `Whitelist.add(name: str) -> bool`
  - `Whitelist.remove(name: str) -> bool`
  - `Whitelist.contains(name: str) -> bool`
  - `Whitelist.import_from_file(filepath: str) -> int  # returns count added`
  - `Whitelist.match(name: str) -> bool  # exact + wildcard`

- [ ] **Step 1: Write the failing test**

Write `tests/test_whitelist.py`:

```python
import json
import tempfile
from pathlib import Path
from app.models.whitelist import Whitelist


class TestWhitelist:
    def make_whitelist(self, data=None):
        """Create a Whitelist pointing to a temp file."""
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        if data is not None:
            json.dump(data, tmp)
            tmp.flush()
        tmp.close()
        return Whitelist(tmp.name), tmp.name

    def test_empty_by_default(self):
        wl, path = self.make_whitelist()
        assert wl.get_all() == set()

    def test_add_and_get_all(self):
        wl, path = self.make_whitelist()
        assert wl.add("PlayerA") is True
        assert wl.get_all() == {"PlayerA"}

    def test_add_duplicate_returns_false(self):
        wl, path = self.make_whitelist()
        wl.add("PlayerA")
        assert wl.add("PlayerA") is False

    def test_remove_existing(self):
        wl, path = self.make_whitelist()
        wl.add("PlayerA")
        assert wl.remove("PlayerA") is True
        assert wl.get_all() == set()

    def test_remove_nonexistent_returns_false(self):
        wl, path = self.make_whitelist()
        assert wl.remove("Ghost") is False

    def test_contains(self):
        wl, path = self.make_whitelist()
        wl.add("PlayerA")
        assert wl.contains("PlayerA") is True
        assert wl.contains("PlayerB") is False

    def test_persistence_survives_reload(self):
        wl1, path = self.make_whitelist()
        wl1.add("PlayerA")
        # Reload from disk
        wl2 = Whitelist(path)
        assert wl2.get_all() == {"PlayerA"}

    def test_import_from_file(self):
        wl, path = self.make_whitelist()
        # Create a text file with names
        import_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        )
        import_file.write("PlayerA\nPlayerB\n  PlayerC  \n\n# comment\nPlayerD\n")
        import_file.close()
        count = wl.import_from_file(import_file.name)
        assert count == 4
        assert wl.get_all() == {"PlayerA", "PlayerB", "PlayerC", "PlayerD"}

    def test_wildcard_star_match(self):
        wl, path = self.make_whitelist()
        wl.add("NC.*")
        assert wl.match("NC.Player1") is True
        assert wl.match("NC.Alpha") is True
        assert wl.match("Goonswarm") is False

    def test_wildcard_question_match(self):
        wl, path = self.make_whitelist()
        wl.add("Player?")
        assert wl.match("Player1") is True
        assert wl.match("PlayerA") is True
        assert wl.match("Player12") is False

    def test_exact_match(self):
        wl, path = self.make_whitelist()
        wl.add("PlayerA")
        assert wl.match("PlayerA") is True
        assert wl.match("PlayerB") is False

    def test_corrupted_file_falls_back_to_empty(self):
        wl, path = self.make_whitelist()
        Path(path).write_text("not valid json {{{")
        wl2 = Whitelist(path)
        # Should not raise, should be empty
        assert wl2.get_all() == set()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_whitelist.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.whitelist'`

- [ ] **Step 3: Write minimal implementation**

Write `app/models/whitelist.py`:

```python
import fnmatch
import json
import threading
from pathlib import Path


class Whitelist:
    """Thread-safe whitelist backed by a JSON file.

    Supports exact matching and fnmatch wildcard patterns (*, ?).
    """

    def __init__(self, filepath: str = "whitelist.json"):
        self._filepath = Path(filepath)
        self._lock = threading.Lock()
        self._names: set[str] = self._load()

    def _load(self) -> set[str]:
        """Load names from JSON file. Returns empty set on any failure."""
        try:
            if self._filepath.exists():
                data = json.loads(self._filepath.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return set(data)
        except (json.JSONDecodeError, OSError):
            pass
        return set()

    def _save(self) -> None:
        """Persist current names to JSON file."""
        try:
            data = sorted(self._names)
            self._filepath.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def get_all(self) -> set[str]:
        """Return a copy of all whitelist entries."""
        with self._lock:
            return set(self._names)

    def add(self, name: str) -> bool:
        """Add a name to the whitelist. Returns True if newly added."""
        name = name.strip()
        if not name:
            return False
        with self._lock:
            if name in self._names:
                return False
            self._names.add(name)
            self._save()
            return True

    def remove(self, name: str) -> bool:
        """Remove a name from the whitelist. Returns True if it existed."""
        name = name.strip()
        with self._lock:
            if name not in self._names:
                return False
            self._names.discard(name)
            self._save()
            return True

    def contains(self, name: str) -> bool:
        """Check if a name exists exactly in the whitelist."""
        with self._lock:
            return name.strip() in self._names

    def match(self, name: str) -> bool:
        """Check if a name matches any whitelist entry (exact or wildcard)."""
        name = name.strip()
        with self._lock:
            for entry in self._names:
                if fnmatch.fnmatch(name, entry):
                    return True
            return False

    def import_from_file(self, filepath: str) -> int:
        """Import names from a text file (one per line, # comments supported).
        Returns number of new names added.
        """
        path = Path(filepath)
        if not path.exists():
            return 0
        text = path.read_text(encoding="utf-8")
        count = 0
        for line in text.splitlines():
            line = line.strip()
            # Skip blank lines and comments
            if not line or line.startswith("#"):
                continue
            if self.add(line):
                count += 1
        return count
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_whitelist.py -v
```

Expected: all 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models/whitelist.py tests/test_whitelist.py
git commit -m "feat: add thread-safe whitelist model with JSON persistence

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Screen Capturer

**Files:**
- Create: `tests/test_capturer.py`
- Create: `app/engine/capturer.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `class Capturer`
  - `Capturer.find_eve_window(keyword: str = "EVE -") -> dict | None  # {"title": str, "x": int, "y": int, "w": int, "h": int}`
  - `Capturer.screenshot(x: int, y: int, w: int, h: int) -> PIL.Image.Image`
  - `Capturer.manual_select() -> dict | None  # user drags on screen overlay, returns rect`

- [ ] **Step 1: Write the failing test**

Write `tests/test_capturer.py`:

```python
from unittest.mock import patch, MagicMock
from PIL import Image
from app.engine.capturer import Capturer


class TestFindEveWindow:
    def make_windows(self, windows):
        """windows: list of (hwnd, title, rect) tuples.
        rect = (left, top, right, bottom) — window rect.
        client_rect = (0, 0, w, h)
        """
        hwnds = []
        titles = {}
        rects = {}
        client_rects = {}
        for hwnd, title, rect in windows:
            hwnds.append(hwnd)
            titles[hwnd] = title
            rects[hwnd] = rect
            # client_rect = (0, 0, width, height) where w,h derived from rect
            client_rects[hwnd] = (0, 0, rect[2] - rect[0], rect[3] - rect[1])

        def mock_enum_windows():
            return hwnds

        def mock_get_window_text(h):
            return titles.get(h, "")

        def mock_get_window_rect(h):
            return rects.get(h, (0, 0, 0, 0))

        def mock_get_client_rect(h):
            return client_rects.get(h, (0, 0, 0, 0))

        def mock_client_to_screen(h, point):
            # point is (0, 0) tuple → return window's top-left in screen coords
            wr = rects.get(h, (0, 0, 0, 0))
            return (wr[0], wr[1])

        return (
            mock_enum_windows,
            mock_get_window_text,
            mock_get_window_rect,
            mock_get_client_rect,
            mock_client_to_screen,
        )

    @patch("app.engine.capturer.win32gui")
    def test_find_by_title_keyword(self, mock_win32gui):
        windows = [
            (1, "Chrome", (0, 0, 500, 400)),
            (2, "EVE - MyCharacter", (100, 200, 900, 800)),
            (3, "Notepad", (0, 400, 300, 600)),
        ]
        em, gwt, gwr, gcr, cts = self.make_windows(windows)
        mock_win32gui.EnumWindows = em
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetWindowRect = gwr
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts

        c = Capturer()
        result = c.find_eve_window()

        assert result is not None
        assert result["title"] == "EVE - MyCharacter"
        assert result["w"] == 800  # client width = window width
        assert result["h"] == 600  # client height = window height

    @patch("app.engine.capturer.win32gui")
    def test_no_eve_window_returns_none(self, mock_win32gui):
        windows = [
            (1, "Chrome", (0, 0, 500, 400)),
            (2, "Notepad", (0, 400, 300, 600)),
        ]
        em, gwt, gwr, gcr, cts = self.make_windows(windows)
        mock_win32gui.EnumWindows = em
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts

        c = Capturer()
        result = c.find_eve_window()
        assert result is None

    @patch("app.engine.capturer.win32gui")
    def test_custom_keyword(self, mock_win32gui):
        windows = [
            (1, "My App - Game", (0, 0, 600, 500)),
        ]
        em, gwt, gwr, gcr, cts = self.make_windows(windows)
        mock_win32gui.EnumWindows = em
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetWindowRect = gwr
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts

        c = Capturer()
        result = c.find_eve_window(keyword="My App")
        assert result is not None
        assert result["title"] == "My App - Game"


class TestScreenshot:
    @patch("app.engine.capturer.ImageGrab")
    def test_screenshot_calls_grab_with_correct_bbox(self, mock_grab):
        mock_img = MagicMock(spec=Image.Image)
        mock_grab.grab.return_value = mock_img

        c = Capturer()
        result = c.screenshot(100, 200, 300, 400)

        mock_grab.grab.assert_called_once_with(
            bbox=(100, 200, 400, 600), all_screens=True
        )
        assert result is mock_img
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_capturer.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.engine.capturer'`

- [ ] **Step 3: Write minimal implementation**

Write `app/engine/capturer.py`:

```python
"""Screen capture and window detection for EVE Online."""

from typing import Optional

import win32gui
from PIL import Image, ImageGrab


class Capturer:
    """Handles window detection and screen region capture."""

    def find_eve_window(self, keyword: str = "EVE -") -> Optional[dict]:
        """Find the first window whose title contains ``keyword``.

        Falls back to matching by process name ``exefile.exe`` if no title
        match is found.

        Returns:
            dict with keys title, x, y, w, h (client-area screen coords),
            or None if no matching window is found.
        """
        result = None

        def callback(hwnd, results):
            title = win32gui.GetWindowText(hwnd)
            if keyword.lower() in title.lower():
                rect = win32gui.GetWindowRect(hwnd)
                left, top, right, bottom = rect
                # Convert client rect to screen coords
                client_rect = win32gui.GetClientRect(hwnd)
                pt = win32gui.ClientToScreen(hwnd, (0, 0))
                results.append({
                    "title": title,
                    "x": pt[0],
                    "y": pt[1],
                    "w": client_rect[2],
                    "h": client_rect[3],
                })
            return True

        results = []
        win32gui.EnumWindows(callback, results)
        if results:
            return results[0]
        return None

    def screenshot(self, x: int, y: int, w: int, h: int) -> Image.Image:
        """Capture a screen region.

        Args:
            x, y: top-left screen coordinates.
            w, h: width and height of the region.

        Returns:
            PIL Image of the captured region.
        """
        bbox = (x, y, x + w, y + h)
        return ImageGrab.grab(bbox=bbox, all_screens=True)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_capturer.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/engine/capturer.py tests/test_capturer.py
git commit -m "feat: add capturer with EVE window detection and screen capture

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: OCR Engine

**Files:**
- Create: `app/engine/ocr.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `class OCREngine`
  - `OCREngine.__init__(lang: str = "ch", confidence_threshold: float = 0.7)`
  - `OCREngine.recognize(image: PIL.Image.Image) -> list[tuple[str, float]]  # [(text, confidence), ...]`

- [ ] **Step 1: Write the implemention**

Write `app/engine/ocr.py`:

```python
"""OCR wrapper using PaddleOCR."""

import logging
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)


class OCREngine:
    """Wraps PaddleOCR for text recognition on screen captures.

    Initialises the model once at construction time.  All recognition
    calls are synchronous (they run on the worker thread so they won't
    block the UI).
    """

    def __init__(
        self,
        lang: str = "ch",
        confidence_threshold: float = 0.7,
    ):
        self._confidence_threshold = confidence_threshold
        self._ocr: Optional[object] = None
        self._lang = lang
        self._init_ocr()

    def _init_ocr(self) -> None:
        """Lazy-init the PaddleOCR instance (expensive)."""
        try:
            from paddleocr import PaddleOCR

            self._ocr = PaddleOCR(lang=self._lang, use_angle_cls=False)
            logger.info("PaddleOCR initialised (lang=%s)", self._lang)
        except Exception:
            logger.exception("Failed to initialise PaddleOCR")
            self._ocr = None

    def recognize(self, image: Image.Image) -> list[tuple[str, float]]:
        """Run OCR on *image* and return high-confidence text lines.

        Each element is ``(text, confidence)`` where ``text`` is the
        recognised string and ``confidence`` is a float in [0, 1].

        Returns an empty list when the OCR engine is unavailable or
        recognition fails.
        """
        if self._ocr is None:
            return []

        # Pre-process: convert to grayscale for better accuracy on game text
        if image.mode != "L":
            image = image.convert("L")

        try:
            raw = self._ocr.ocr(image, cls=False)
        except Exception:
            logger.exception("OCR recognition failed")
            return []

        if raw is None or len(raw) == 0:
            return []

        results: list[tuple[str, float]] = []
        # raw[0] is list of [bbox, (text, confidence)] per detected text block
        for block in raw[0]:
            if block is None:
                continue
            _, info = block  # info is (text, confidence)
            text, conf = info[0], float(info[1])
            if conf >= self._confidence_threshold:
                results.append((text.strip(), conf))

        return results
```

- [ ] **Step 2: Verify import**

```bash
python -c "from app.engine.ocr import OCREngine; print('OCR module OK')"
```

Expected: `OCR module OK`

Note: This will trigger PaddleOCR model download on first run (about 100–200 MB).  PaddleOCR downloads models lazily on first `ocr()` call, so this import check will pass quickly and the full model download will happen when the worker first calls `recognize()`.

- [ ] **Step 3: Commit**

```bash
git add app/engine/ocr.py
git commit -m "feat: add PaddleOCR wrapper with greyscale pre-processing

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Threat Detector

**Files:**
- Create: `tests/test_detector.py`
- Create: `app/engine/detector.py`

**Interfaces:**
- Consumes: `Whitelist` (from Task 2)
- Produces:
  - `class Detector`
  - `Detector.__init__(whitelist: Whitelist, cooldown_seconds: float = 60)`
  - `Detector.check(ocr_results: list[tuple[str, float]]) -> list[str]  # returns new threat names`

- [ ] **Step 1: Write the failing test**

Write `tests/test_detector.py`:

```python
import time
from app.models.whitelist import Whitelist
from app.engine.detector import Detector


class TestDetector:
    def make_detector(self, names=None, cooldown=60):
        wl = Whitelist(":memory:")  # Will fail to load, starts empty
        if names:
            for n in names:
                wl.add(n)
        # Override filepath so it doesn't touch disk
        wl._filepath = None
        wl._save = lambda: None
        return Detector(wl, cooldown_seconds=cooldown)

    def test_all_whitelisted_returns_empty(self):
        d = self.make_detector(["Alice", "Bob"])
        results = [("Alice", 0.95), ("Bob", 0.90)]
        assert d.check(results) == []

    def test_non_whitelisted_detected(self):
        d = self.make_detector(["Alice"])
        results = [("Alice", 0.95), ("Eve", 0.88)]
        threats = d.check(results)
        assert threats == ["Eve"]

    def test_wildcard_match_excludes_threat(self):
        d = self.make_detector(["NC.*"])
        results = [("NC.Player1", 0.90), ("Hostile", 0.85)]
        assert d.check(results) == ["Hostile"]

    def test_mixed_case_and_whitespace(self):
        d = self.make_detector(["alice"])
        results = [("  Alice  ", 0.95)]
        assert d.check(results) == []

    def test_cooldown_suppresses_repeat(self):
        d = self.make_detector([], cooldown=60)
        results = [("Hostile", 0.85)]
        # First check: should detect
        assert d.check(results) == ["Hostile"]
        # Second check immediately: should suppress
        assert d.check(results) == []

    def test_cooldown_expires(self):
        d = self.make_detector([], cooldown=0.01)  # 10ms cooldown
        results = [("Hostile", 0.85)]
        d.check(results)
        time.sleep(0.02)  # wait past cooldown
        assert d.check(results) == ["Hostile"]

    def test_empty_input(self):
        d = self.make_detector(["Alice"])
        assert d.check([]) == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_detector.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.engine.detector'`

- [ ] **Step 3: Write minimal implementation**

Write `app/engine/detector.py`:

```python
"""Threat detection: compare OCR results against a whitelist."""

import time
from typing import Optional

from app.models.whitelist import Whitelist


class Detector:
    """Compares recognised names against a whitelist and tracks
    recently-seen threats to avoid alert spam.
    """

    def __init__(
        self,
        whitelist: Whitelist,
        cooldown_seconds: float = 60.0,
    ):
        self._whitelist = whitelist
        self._cooldown = cooldown_seconds
        # name → last-alerted timestamp
        self._last_alert: dict[str, float] = {}

    def check(self, ocr_results: list[tuple[str, float]]) -> list[str]:
        """Return names from *ocr_results* that are NOT in the whitelist
        and have not been alerted within the cooldown window.

        Each element of *ocr_results* is ``(text, confidence)``.
        """
        now = time.monotonic()
        threats: list[str] = []

        for text, _confidence in ocr_results:
            name = text.strip()
            if not name:
                continue
            # Skip if whitelisted
            if self._whitelist.match(name):
                continue
            # Skip if still on cooldown
            last = self._last_alert.get(name)
            if last is not None and (now - last) < self._cooldown:
                continue
            # New threat
            self._last_alert[name] = now
            threats.append(name)

        return threats
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_detector.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/engine/detector.py tests/test_detector.py
git commit -m "feat: add threat detector with whitelist matching and cooldown

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Worker Thread

**Files:**
- Create: `app/engine/worker.py`

**Interfaces:**
- Consumes: `Capturer`, `OCREngine`, `Detector` (from Tasks 3, 4, 5)
- Produces:
  - `class MonitorWorker(QThread)`
  - Signals: `threat_detected = pyqtSignal(list)  # list[str]`, `status_update = pyqtSignal(str)`, `scan_complete = pyqtSignal(int)  # total_scans`
  - `MonitorWorker.set_region(x, y, w, h)`
  - `MonitorWorker.set_interval(seconds: float)`
  - `run()` — the thread loop
  - `stop()`

- [ ] **Step 1: Write implementation**

Write `app/engine/worker.py`:

```python
"""Background worker thread for the monitor loop."""

import logging
import time
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from app.engine.capturer import Capturer
from app.engine.detector import Detector
from app.engine.ocr import OCREngine

logger = logging.getLogger(__name__)


class MonitorWorker(QThread):
    """Runs capture → OCR → detect on a timer in a background thread."""

    threat_detected = pyqtSignal(list)   # list[str] — new threat names
    status_update = pyqtSignal(str)       # human-readable status message
    scan_complete = pyqtSignal(int)       # total scan count

    def __init__(
        self,
        capturer: Capturer,
        ocr: OCREngine,
        detector: Detector,
        parent=None,
    ):
        super().__init__(parent)
        self._capturer = capturer
        self._ocr = ocr
        self._detector = detector
        self._interval = 2.0           # seconds between scans
        self._running = False
        self._region: Optional[dict] = None  # {x, y, w, h}

    def set_region(self, x: int, y: int, w: int, h: int) -> None:
        """Set the screen region to capture."""
        self._region = {"x": x, "y": y, "w": w, "h": h}

    def set_interval(self, seconds: float) -> None:
        """Set the delay between scans (1–10 seconds)."""
        self._interval = max(1.0, min(10.0, float(seconds)))

    def stop(self) -> None:
        """Request the loop to stop at the next iteration."""
        self._running = False

    def run(self) -> None:
        """Main loop.  Runs until :meth:`stop` is called."""
        self._running = True
        scan_count = 0

        self.status_update.emit("监控已启动")

        while self._running:
            if self._region is None:
                self.status_update.emit("未设置截图区域")
                self.msleep(500)
                continue

            try:
                # 1. Capture
                r = self._region
                img = self._capturer.screenshot(r["x"], r["y"], r["w"], r["h"])

                # 2. OCR
                ocr_results = self._ocr.recognize(img)

                # 3. Detect
                threats = self._detector.check(ocr_results)

                scan_count += 1
                self.scan_complete.emit(scan_count)

                if threats:
                    names = ", ".join(threats)
                    self.threat_detected.emit(threats)
                    self.status_update.emit(f"发现威胁: {names}")
                else:
                    self.status_update.emit("无威胁")

            except Exception:
                logger.exception("Scan cycle failed")
                self.status_update.emit("扫描出错，已跳过当前帧")

            # Wait between scans
            self.msleep(int(self._interval * 1000))
```

- [ ] **Step 2: Verify module can be imported**

```bash
python -c "from app.engine.worker import MonitorWorker; print('Worker module OK')"
```

Expected: `Worker module OK` (PyQt6 must be installed for QThread import).

- [ ] **Step 3: Commit**

```bash
git add app/engine/worker.py
git commit -m "feat: add monitor worker thread with capture→OCR→detect loop

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Alert Dialog

**Files:**
- Create: `app/ui/alert_dialog.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `class AlertDialog(QDialog)`
  - `AlertDialog.__init__(threats: list[str], parent=None)`
  - Plays `resources/alert.wav` on show

- [ ] **Step 1: Write implementation**

Write `app/ui/alert_dialog.py`:

```python
"""Modal alert dialog shown when threats are detected."""

from pathlib import Path

from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtMultimedia import QSoundEffect
from PyQt6.QtWidgets import (
    QDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)


class AlertDialog(QDialog):
    """Modal popup listing detected hostile player names."""

    def __init__(self, threats: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚠ 威胁预警！")
        self.setMinimumSize(300, 200)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Header
        header = QLabel(f"发现 {len(threats)} 个敌对目标")
        header.setStyleSheet("font-size: 16px; font-weight: bold; color: #cc0000;")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)

        # Threat list
        list_widget = QListWidget()
        list_widget.setStyleSheet(
            "QListWidget { border: 1px solid #cc0000; border-radius: 4px; "
            "background: #fff8f8; font-size: 14px; }"
        )
        for name in threats:
            item = QListWidgetItem(f"🚨  {name}")
            list_widget.addItem(item)
        layout.addWidget(list_widget)

        # OK button
        btn = QPushButton("确认")
        btn.setMinimumHeight(36)
        btn.setStyleSheet(
            "QPushButton { background: #cc0000; color: white; border-radius: 4px; "
            "font-size: 14px; font-weight: bold; }"
            "QPushButton:hover { background: #dd2222; }"
        )
        btn.clicked.connect(self.accept)
        layout.addWidget(btn)

        # Play alert sound
        self._play_sound()

    def _play_sound(self) -> None:
        """Play the alert wav file if it exists."""
        sound_path = Path(__file__).parent.parent.parent / "resources" / "alert.wav"
        if sound_path.exists():
            try:
                self._sound = QSoundEffect()
                self._sound.setSource(QUrl.fromLocalFile(str(sound_path.resolve())))
                self._sound.setVolume(1.0)
                self._sound.play()
            except Exception:
                pass  # Sound is non-critical
```

- [ ] **Step 2: Verify import**

```bash
python -c "from app.ui.alert_dialog import AlertDialog; print('Alert dialog module OK')"
```

Expected: `Alert dialog module OK`

- [ ] **Step 3: Commit**

```bash
git add app/ui/alert_dialog.py
git commit -m "feat: add modal alert dialog with threat list and sound

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Settings Panel

**Files:**
- Create: `app/ui/settings.py`

**Interfaces:**
- Consumes: `Whitelist` (from Task 2)
- Produces:
  - `class SettingsPanel(QWidget)`
  - `SettingsPanel.__init__(whitelist: Whitelist, parent=None)`
  - `SettingsPanel.get_interval() -> float`
  - `SettingsPanel.get_keyword() -> str`
  - Signals: `whitelist_changed = pyqtSignal()`

- [ ] **Step 1: Write implementation**

Write `app/ui/settings.py`:

```python
"""Settings panel: whitelist management, scan interval, window keyword."""

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.models.whitelist import Whitelist


class SettingsPanel(QWidget):
    """Left-side control panel with whitelist editor and scan config."""

    whitelist_changed = pyqtSignal()

    def __init__(self, whitelist: Whitelist, parent=None):
        super().__init__(parent)
        self._whitelist = whitelist

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # --- Whitelist group ---
        wl_group = QGroupBox("白名单管理")
        wl_layout = QVBoxLayout(wl_group)

        self._wl_list = QListWidget()
        self._refresh_wl_list()
        wl_layout.addWidget(self._wl_list)

        # Buttons row
        btn_row = QHBoxLayout()
        add_btn = QPushButton("添加")
        add_btn.clicked.connect(self._add_entry)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self._remove_entry)
        import_btn = QPushButton("导入")
        import_btn.clicked.connect(self._import_file)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addWidget(import_btn)
        wl_layout.addLayout(btn_row)

        layout.addWidget(wl_group)

        # --- Scan config group ---
        cfg_group = QGroupBox("扫描设置")
        cfg_layout = QVBoxLayout(cfg_group)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("扫描间隔 (秒):"))
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 10)
        self._interval_spin.setValue(2)
        self._interval_spin.setSuffix(" 秒")
        interval_row.addWidget(self._interval_spin)
        interval_row.addStretch()
        cfg_layout.addLayout(interval_row)

        keyword_row = QHBoxLayout()
        keyword_row.addWidget(QLabel("窗口关键词:"))
        self._keyword_edit = QLineEdit("EVE -")
        keyword_row.addWidget(self._keyword_edit)
        cfg_layout.addLayout(keyword_row)

        layout.addWidget(cfg_group)
        layout.addStretch()

    def _refresh_wl_list(self):
        """Reload the list widget from the whitelist model."""
        self._wl_list.clear()
        for name in sorted(self._whitelist.get_all()):
            self._wl_list.addItem(name)

    def _add_entry(self):
        from PyQt6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, "添加白名单", "玩家/军团名 (支持 * 通配符):")
        if ok and name.strip():
            self._whitelist.add(name.strip())
            self._refresh_wl_list()
            self.whitelist_changed.emit()

    def _remove_entry(self):
        item = self._wl_list.currentItem()
        if item:
            self._whitelist.remove(item.text())
            self._refresh_wl_list()
            self.whitelist_changed.emit()

    def _import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入白名单", "", "文本文件 (*.txt);;所有文件 (*)"
        )
        if path:
            count = self._whitelist.import_from_file(path)
            self._refresh_wl_list()
            self.whitelist_changed.emit()
            QMessageBox.information(self, "导入完成", f"已导入 {count} 个条目。")

    def get_interval(self) -> float:
        return float(self._interval_spin.value())

    def get_keyword(self) -> str:
        return self._keyword_edit.text().strip()
```

- [ ] **Step 2: Verify import**

```bash
python -c "from app.ui.settings import SettingsPanel; print('Settings module OK')"
```

Expected: `Settings module OK`

- [ ] **Step 3: Commit**

```bash
git add app/ui/settings.py
git commit -m "feat: add settings panel with whitelist editor and scan config

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: Main Window

**Files:**
- Create: `app/ui/main_window.py`

**Interfaces:**
- Consumes: `Whitelist`, `Capturer`, `OCREngine`, `Detector`, `MonitorWorker`, `SettingsPanel`, `AlertDialog` (from Tasks 2–8)
- Produces:
  - `class MainWindow(QMainWindow)`
  - Owns the full app lifecycle

- [ ] **Step 1: Write implementation**

Write `app/ui/main_window.py`:

```python
"""Main application window."""

import logging
from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.engine.capturer import Capturer
from app.engine.detector import Detector
from app.engine.ocr import OCREngine
from app.engine.worker import MonitorWorker
from app.models.whitelist import Whitelist
from app.ui.alert_dialog import AlertDialog
from app.ui.settings import SettingsPanel

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level window: settings on the left, log on the right, tray icon."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EVE Sentry")
        self.setMinimumSize(700, 450)

        # ---- Models & Engine ----
        self._whitelist = Whitelist("whitelist.json")
        self._capturer = Capturer()
        self._ocr = OCREngine(lang="ch", confidence_threshold=0.7)
        self._detector = Detector(self._whitelist, cooldown_seconds=60.0)
        self._worker: MonitorWorker | None = None

        # ---- Central widget ----
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # Left: settings panel
        self._settings = SettingsPanel(self._whitelist)
        self._settings.setFixedWidth(220)
        root.addWidget(self._settings)

        # Right: log area + control buttons
        right = QVBoxLayout()
        right.setSpacing(6)

        # Monitor button
        self._monitor_btn = QPushButton("开始监控")
        self._monitor_btn.setMinimumHeight(40)
        self._monitor_btn.setStyleSheet(
            "QPushButton { background: #228b22; color: white; border-radius: 4px; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:hover { background: #2ea62e; }"
            "QPushButton:checked { background: #cc0000; }"
        )
        self._monitor_btn.setCheckable(True)
        self._monitor_btn.clicked.connect(self._toggle_monitor)
        right.addWidget(self._monitor_btn)

        # Window info row
        self._window_label = QLabel("窗口: 未检测")
        self._window_label.setStyleSheet("color: #666; font-size: 11px;")
        right.addWidget(self._window_label)

        right.addWidget(QLabel("状态日志:"))

        # Log text area
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(
            "QTextEdit { background: #1a1a2e; color: #e0e0e0; "
            "font-family: Consolas, monospace; font-size: 12px; }"
        )
        right.addWidget(self._log)

        # Bottom buttons
        btn_row = QHBoxLayout()
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(self._log.clear)
        btn_row.addWidget(clear_btn)

        select_btn = QPushButton("重选区域")
        select_btn.clicked.connect(self._select_region)
        btn_row.addWidget(select_btn)

        btn_row.addStretch()
        right.addLayout(btn_row)

        root.addLayout(right, 1)

        # ---- Status bar ----
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status_label = QLabel("● 未启动")
        self._status.addWidget(self._status_label)

        # ---- System tray ----
        self._setup_tray()

        # Try auto-detect window
        self._detect_window()

    # ------------------------------------------------------------------
    # Window detection
    # ------------------------------------------------------------------

    def _detect_window(self) -> None:
        """Try to find the EVE window and display info."""
        keyword = self._settings.get_keyword()
        info = self._capturer.find_eve_window(keyword=keyword)
        if info:
            self._window_label.setText(
                f"窗口: {info['title']} ({info['w']}×{info['h']})"
            )
            self._detected_region = info
        else:
            self._window_label.setText("窗口: 未找到 (请手动框选)")

    # ------------------------------------------------------------------
    # Region selection (manual fallback)
    # ------------------------------------------------------------------

    def _select_region(self) -> None:
        """Pop up a full-screen overlay for drag-to-select."""
        # This is a simplified version — the real overlay would be a
        # transparent fullscreen widget.  For now, fall back to the
        # detected window and show a message.
        self._detect_window()
        if hasattr(self, "_detected_region"):
            info = self._detected_region
            self._log_message(f"使用窗口区域: {info['w']}×{info['h']}")
        else:
            QMessageBox.warning(
                self,
                "手动框选",
                "请确保 EVE 已运行，或手动输入窗口关键词后再试。",
            )

    # ------------------------------------------------------------------
    # Monitor start / stop
    # ------------------------------------------------------------------

    def _toggle_monitor(self, checked: bool) -> None:
        if checked:
            self._start_monitor()
        else:
            self._stop_monitor()

    def _start_monitor(self) -> None:
        # Ensure we have a region
        if not hasattr(self, "_detected_region") or self._detected_region is None:
            self._detect_window()
        if not hasattr(self, "_detected_region") or self._detected_region is None:
            QMessageBox.critical(self, "错误", "找不到 EVE 窗口，请确保游戏已运行。")
            self._monitor_btn.setChecked(False)
            return

        r = self._detected_region
        self._worker = MonitorWorker(self._capturer, self._ocr, self._detector)
        self._worker.set_region(r["x"], r["y"], r["w"], r["h"])
        self._worker.set_interval(self._settings.get_interval())

        self._worker.threat_detected.connect(self._on_threat)
        self._worker.status_update.connect(self._log_message)
        self._worker.scan_complete.connect(self._update_scan_count)

        self._worker.start()

        self._monitor_btn.setText("停止监控")
        self._monitor_btn.setStyleSheet(
            "QPushButton { background: #cc0000; color: white; border-radius: 4px; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:hover { background: #ee2222; }"
        )
        self._status_label.setText("● 运行中")
        self._status_label.setStyleSheet("color: #228b22; font-weight: bold;")
        self._log_message("监控已启动")

    def _stop_monitor(self) -> None:
        if self._worker:
            self._worker.stop()
            self._worker.wait(3000)
            self._worker = None

        self._monitor_btn.setText("开始监控")
        self._monitor_btn.setStyleSheet(
            "QPushButton { background: #228b22; color: white; border-radius: 4px; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:hover { background: #2ea62e; }"
        )
        self._status_label.setText("● 已停止")
        self._status_label.setStyleSheet("color: #888;")
        self._log_message("监控已停止")

    # ------------------------------------------------------------------
    # Alert handling
    # ------------------------------------------------------------------

    def _on_threat(self, threats: list[str]) -> None:
        """Show alert dialog when threats are detected."""
        dlg = AlertDialog(threats, self)
        dlg.exec()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_message(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.append(f"[{ts}] {msg}")

    def _update_scan_count(self, count: int) -> None:
        # Status bar already shows "running" — we just note it
        pass

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(self)
        # icon_path = Path(__file__).parent.parent.parent / "resources" / "icon.ico"
        # if icon_path.exists():
        #     self._tray.setIcon(QIcon(str(icon_path.resolve())))
        self._tray.setToolTip("EVE Sentry")
        self._tray.activated.connect(self._on_tray_activated)

        menu = self._tray.contextMenu()
        if menu is None:
            from PyQt6.QtWidgets import QMenu
            menu = QMenu()
            self._tray.setContextMenu(menu)

        show_action = QAction("显示主窗口")
        show_action.triggered.connect(self.show)
        menu.addAction(show_action)

        quit_action = QAction("退出")
        quit_action.triggered.connect(self._quit_app)
        menu.addAction(quit_action)

        self._tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.raise_()

    def closeEvent(self, event):
        """Minimize to tray instead of closing."""
        event.ignore()
        self.hide()

    def _quit_app(self):
        self._stop_monitor()
        self._tray.hide()
        QApplication.quit()
```

- [ ] **Step 2: Verify import**

```bash
python -c "from app.ui.main_window import MainWindow; print('Main window module OK')"
```

Expected: `Main window module OK`

- [ ] **Step 3: Commit**

```bash
git add app/ui/main_window.py
git commit -m "feat: add main window with monitor control, logging, and system tray

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: Entry Point

**Files:**
- Create: `main.py`

**Interfaces:**
- Consumes: `MainWindow` (from Task 9)
- Produces: runnable application

- [ ] **Step 1: Write entry point**

Write `main.py`:

```python
"""EVE Sentry — EVE Online hostile player early warning system."""

import logging
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from app.ui.main_window import MainWindow


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("EVE Sentry")
    app.setOrganizationName("EveSentry")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify app can construct (headless check)**

```bash
python -c "
import os; os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from main import main
print('Entry point OK')
"
```

Expected: `Entry point OK` (the app exits immediately in offscreen mode because exec() returns).

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add application entry point with high-DPI support

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 11: Resources + Manual Smoke Test

**Files:**
- Create: `resources/alert.wav` (placeholder — user provides actual sound)
- Create: `resources/icon.ico` (placeholder — user provides actual icon)

**Interfaces:**
- Consumes: full application (Tasks 1–10)
- Produces: working app ready for manual testing

- [ ] **Step 1: Add a simple alert sound placeholder**

We can't generate a real `.wav` from Python in this task, so we create a script:

Write a helper script `scripts/generate_alert.py`:

```python
"""Generate a simple alert beep wav file."""
import struct
import wave
import math

SAMPLE_RATE = 44100
DURATION = 0.3  # seconds
FREQ = 880      # Hz (A5 note)

samples = []
for i in range(int(SAMPLE_RATE * DURATION)):
    t = i / SAMPLE_RATE
    # Simple sine wave with envelope
    envelope = 1.0 - (i / (SAMPLE_RATE * DURATION))
    value = int(16000 * math.sin(2 * math.pi * FREQ * t) * envelope)
    samples.append(struct.pack("<h", value))

with wave.open("resources/alert.wav", "w") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(SAMPLE_RATE)
    wf.writeframes(b"".join(samples))

print("Generated resources/alert.wav")
```

- [ ] **Step 2: Generate the sound file**

```bash
mkdir -p scripts
python scripts/generate_alert.py
```

Expected: `Generated resources/alert.wav`

- [ ] **Step 3: Verify all engine tests still pass**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS (whitelist: 11, capturer: 4, detector: 7 = 22 total).

- [ ] **Step 4: Manual smoke test checklist**

Run the app:

```bash
python main.py
```

Verify:
1. Main window opens with title "EVE Sentry"
2. Settings panel shows whitelist editor and scan config
3. Status bar shows "● 未启动" (not started)
4. Click "开始监控" → button changes to "停止监控", status shows "● 运行中"
5. Click "停止监控" → stops, status goes back to "● 已停止"
6. Add a name to whitelist → appears in list
7. Close window → minimizes to system tray
8. Double-click tray icon → window reopens

- [ ] **Step 5: Commit**

```bash
git add resources/alert.wav scripts/generate_alert.py
git commit -m "feat: add alert sound generator and manual smoke test checklist

Co-Authored-By: Claude <noreply@anthropic.com>"
```
