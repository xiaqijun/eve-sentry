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
