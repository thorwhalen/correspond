"""State stores under the data root."""

import os

import pytest


@pytest.mark.skipif(os.name == "nt", reason="Windows folders have no POSIX mode bits")
def test_a_new_state_folder_is_the_owners_only(tmp_path):
    import stat

    from correspond.stores import json_store

    store = json_store("example", data_dir=tmp_path)
    store["k"] = {"v": 1}
    assert stat.S_IMODE((tmp_path / "example").stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="Windows folders have no POSIX mode bits")
def test_an_existing_open_state_folder_is_tightened_on_first_use(tmp_path):
    import stat

    from correspond.stores import json_store

    folder = tmp_path / "example"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    json_store("example", data_dir=tmp_path)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_arrival_stamps_increase_across_handles_and_writers_take_turns(tmp_path):
    import threading
    import time

    from correspond.stores import arrival_stamps

    path = arrival_stamps("example", data_dir=tmp_path).path
    assert path.parent == tmp_path / "locks"
    with arrival_stamps("example", data_dir=tmp_path).issue(100) as first:
        pass
    # a second writer (its own handle on the file) whose clock is behind
    with arrival_stamps("example", data_dir=tmp_path).issue(50) as second:
        pass
    assert (first, second) == (100, 101)

    inside, overlapped, issued = [0], [False], []

    def write():
        with arrival_stamps("example", data_dir=tmp_path).issue(0) as stamp:
            inside[0] += 1
            time.sleep(0.01)  # give another writer the chance to barge in
            overlapped[0] |= inside[0] > 1
            issued.append(stamp)
            inside[0] -= 1

    threads = [threading.Thread(target=write) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not overlapped[0]
    assert sorted(issued) == list(range(102, 110))


def test_arrival_stamps_refuse_a_kind_that_is_not_a_name(tmp_path):
    from correspond.stores import arrival_stamps

    with pytest.raises(ValueError):
        arrival_stamps("../elsewhere", data_dir=tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="Windows folders have no POSIX mode bits")
def test_a_linked_state_folder_is_left_alone(tmp_path):
    import stat

    from correspond.stores import private_dir

    target = tmp_path / "elsewhere"
    target.mkdir()
    target.chmod(0o755)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "example").symlink_to(target)
    private_dir("example", data_dir=tmp_path / "data")
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_a_reader_holding_the_lock_takes_no_stamp(tmp_path):
    from correspond.stores import arrival_stamps

    stamps = arrival_stamps("example", data_dir=tmp_path)
    with stamps.hold():
        pass
    with stamps.issue(1) as stamp:
        pass
    assert stamp == 1
