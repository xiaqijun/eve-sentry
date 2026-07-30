import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.single_instance import SingleInstanceGuard


def test_single_instance_guard_notifies_primary_instance():
    app = QApplication.instance() or QApplication([])
    name = f"EveSentry-Test-{os.getpid()}"
    primary = SingleInstanceGuard(name, parent=app)
    secondary = SingleInstanceGuard(name, parent=app)
    activations = []
    primary.activate_requested.connect(lambda: activations.append(True))
    try:
        assert primary.acquire() is True
        assert secondary.acquire() is False
        app.processEvents()
        assert activations == [True]
    finally:
        secondary.close()
        primary.close()
