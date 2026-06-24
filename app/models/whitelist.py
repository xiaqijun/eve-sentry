import fnmatch
import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


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
            logger.exception("Failed to save whitelist to %s", self._filepath)

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
