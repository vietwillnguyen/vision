import json
import os
import threading

import pytest

from visio_recorder import private_files
from visio_recorder.private_files import write_private_text_atomic
from visio_recorder.session_store import session_persister


class _Session:
    def __init__(self, n: int) -> None:
        # Long enough that a torn or truncated read would show.
        self.access_token = f"at-{n}-" + "a" * 2000
        self.refresh_token = f"rt-{n}"


def test_concurrent_persisters_on_one_path_never_tear_it(tmp_path):
    # Two live clients each kept a persister on session.json, and a shared
    # temp name let one writer rename or truncate the other's file: that
    # raised FileNotFoundError and could publish an empty session.json.
    path = tmp_path / "session.json"
    write_private_text_atomic(
        path, json.dumps({"access_token": "a", "refresh_token": "r"})
    )
    persisters = [session_persister(path) for _ in range(4)]
    errors: list[BaseException] = []
    torn: list[str] = []
    done = threading.Event()

    def write(persist, base: int) -> None:
        try:
            for n in range(base, base + 60):
                persist("TOKEN_REFRESHED", _Session(n))
        except BaseException as err:  # noqa: BLE001 - reported below
            errors.append(err)

    def read() -> None:
        while not done.is_set():
            text = path.read_text()
            try:
                data = json.loads(text)
                if not data["refresh_token"]:
                    torn.append(text)
            except (ValueError, KeyError):
                torn.append(text)

    reader = threading.Thread(target=read)
    reader.start()
    writers = [
        threading.Thread(target=write, args=(persist, i * 1000))
        for i, persist in enumerate(persisters)
    ]
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    done.set()
    reader.join()

    assert errors == []
    assert torn == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["session.json"]
    assert json.loads(path.read_text())["refresh_token"].startswith("rt-")


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    write_private_text_atomic(path, "old")

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(private_files.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        write_private_text_atomic(path, "new")

    assert path.read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_writes_to_different_paths_do_not_wait_for_each_other(tmp_path):
    # The lock is per path: a slow session.json write must not hold up
    # pairing.json or device.json.
    lock = private_files._lock_for(tmp_path / "a.json")

    with lock:
        finished = threading.Event()

        def other() -> None:
            write_private_text_atomic(tmp_path / "b.json", "b")
            finished.set()

        t = threading.Thread(target=other)
        t.start()
        assert finished.wait(5)
        t.join()
    assert os.path.exists(tmp_path / "b.json")
