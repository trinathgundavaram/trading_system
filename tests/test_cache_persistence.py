import threading
import time

from engine.cache import TTLCache


def test_value_survives_a_new_process(tmp_path):
    a = TTLCache(tmp_path)
    a.set("k", {"x": [1, 2, 3]}, 60)
    b = TTLCache(tmp_path)                      # a later cycle's fresh process
    assert b.get("k") == {"x": [1, 2, 3]}


def test_expired_entries_are_not_returned(tmp_path):
    a = TTLCache(tmp_path)
    a.set("k", "v", 0.05)
    time.sleep(0.1)
    assert TTLCache(tmp_path).get("k") is None
    assert a.get("k") is None


def test_unpicklable_value_falls_back_to_memory(tmp_path):
    a = TTLCache(tmp_path)
    a.set("k", threading.Lock(), 60)             # cannot be pickled
    assert a.get("k") is not None                # still served from memory
    assert TTLCache(tmp_path).get("k") is None   # but not persisted


def test_corrupt_file_is_a_miss(tmp_path):
    a = TTLCache(tmp_path)
    a.set("k", "v", 60)
    for p in tmp_path.glob("*.pkl"):
        p.write_bytes(b"not a pickle")
    assert TTLCache(tmp_path).get("k") is None


def test_clear_removes_disk_entries(tmp_path):
    a = TTLCache(tmp_path)
    a.set("k", "v", 60)
    a.clear()
    assert TTLCache(tmp_path).get("k") is None
