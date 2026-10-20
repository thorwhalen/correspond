"""State stores under the data root."""


def test_a_new_state_folder_is_the_owners_only(tmp_path):
    import stat

    from correspond.stores import json_store

    store = json_store("example", data_dir=tmp_path)
    store["k"] = {"v": 1}
    assert stat.S_IMODE((tmp_path / "example").stat().st_mode) == 0o700
