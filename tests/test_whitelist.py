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
