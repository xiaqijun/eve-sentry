import time

from app.server.esi_worker import EsiWorker


def test_worker_exits_after_becoming_idle():
    completed: list[str] = []
    worker = EsiWorker(completed.append)

    assert worker.submit("task", "payload") is True
    assert worker.wait_idle(timeout=1) is True
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        thread = worker._thread
        if thread is None or not thread.is_alive():
            break
        time.sleep(0.02)

    assert completed == ["payload"]
    assert worker._thread is None
    worker.close()
