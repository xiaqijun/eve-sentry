from app.ui import alert_dialog as alert_dialog_module
from app.ui.alert_dialog import AlertDialog


class DummyDialog:
    pass


def test_alert_dialog_play_sound_uses_alert_wav(monkeypatch, tmp_path):
    class FakeQUrl:
        @staticmethod
        def fromLocalFile(path):
            return ("local-file", path)

    class FakeSoundEffect:
        instances = []

        def __init__(self):
            self.source = None
            self.volume = None
            self.played = False
            self.instances.append(self)

        def setSource(self, source):
            self.source = source

        def setVolume(self, volume):
            self.volume = volume

        def play(self):
            self.played = True

    module_file = tmp_path / "app" / "ui" / "alert_dialog.py"
    sound_path = tmp_path / "resources" / "alert.wav"
    module_file.parent.mkdir(parents=True)
    sound_path.parent.mkdir(parents=True)
    module_file.write_text("", encoding="utf-8")
    sound_path.write_bytes(b"")

    monkeypatch.setattr(alert_dialog_module, "__file__", str(module_file))
    monkeypatch.setattr(alert_dialog_module, "QUrl", FakeQUrl)
    monkeypatch.setattr(alert_dialog_module, "QSoundEffect", FakeSoundEffect)

    dialog = DummyDialog()
    AlertDialog._play_sound(dialog)

    assert len(FakeSoundEffect.instances) == 1
    sound = FakeSoundEffect.instances[0]
    assert sound.source == ("local-file", str(sound_path.resolve()))
    assert sound.volume == 1.0
    assert sound.played is True
    assert dialog._sound is sound


def test_alert_dialog_play_sound_is_non_critical(monkeypatch, tmp_path):
    class BrokenSoundEffect:
        def setSource(self, source):
            _ = source

        def setVolume(self, volume):
            _ = volume

        def play(self):
            raise RuntimeError("audio unavailable")

    module_file = tmp_path / "app" / "ui" / "alert_dialog.py"
    sound_path = tmp_path / "resources" / "alert.wav"
    module_file.parent.mkdir(parents=True)
    sound_path.parent.mkdir(parents=True)
    module_file.write_text("", encoding="utf-8")
    sound_path.write_bytes(b"")

    monkeypatch.setattr(alert_dialog_module, "__file__", str(module_file))
    monkeypatch.setattr(alert_dialog_module, "QSoundEffect", BrokenSoundEffect)

    dialog = DummyDialog()
    AlertDialog._play_sound(dialog)
