"""WhatsApp from chat exports: both layouts parsed, overlapping exports merged once, the archive read with since."""

import io
import zipfile

import pytest

from correspond import tools
from correspond.channels.whatsapp import WhatsAppExport, export_text, parse_export
from correspond.errors import InvalidRef

IOS = (
    "‎[2/11/26, 12:51:08] Ada Example: ‎Messages and calls are end-to-end encrypted.\n"
    "[2/11/26, 12:51:27] Ada Example: hello\n"
    "a second line\n"
    "[2/11/26, 12:54:26] Bram: hi! <This message was edited>\n"
    "[2/13/26, 9:05:00] Ada Example: ‎image omitted\n"
    "[2/13/26, 9:06:00] Bram: ‎Missed voice call\n"
    "[2/13/26, 9:07:00] Ada Example: This message was deleted.\n"
)
LATER = IOS + "[3/1/26, 18:40:00] Bram: the next round\n"
ANDROID = (
    "13/02/2026, 21:05 - Messages and calls are end-to-end encrypted.\n"
    "13/02/2026, 21:06 - Ada Example: <Media omitted>\n"
    "13/02/2026, 9:07 pm - Bram: late: with a colon\n"
)


def test_ios_export_rows_carry_time_sender_text_and_markers():
    rows = parse_export(IOS, tz="UTC")
    assert [r["sender"] for r in rows] == ["Ada Example", "Ada Example", "Bram", "Ada Example", "Bram", "Ada Example"]
    assert rows[1]["text"] == "hello\na second line" and rows[1]["native"]["line"] == 2
    assert rows[2]["text"] == "hi!" and rows[2]["native"]["edited"] is True
    assert rows[3]["native"]["media"] == "image" and rows[3]["text"] == ""
    assert rows[4]["native"]["call"] is True
    assert rows[5]["native"]["deleted"] is True
    assert rows[3]["sent_at"] == "2026-02-13T09:05:00+00:00"  # month first: 13 is a day


def test_android_export_day_first_system_lines_and_twelve_hour_times():
    rows = parse_export(ANDROID, tz="UTC")
    assert rows[0]["sender"] == "" and rows[0]["native"]["system"] is True
    assert rows[1]["native"]["media"] == "media"
    assert rows[2]["sent_at"] == "2026-02-13T21:07:00+00:00"
    assert rows[2]["sender"] == "Bram" and rows[2]["text"] == "late: with a colon"


def test_ambiguous_dates_follow_the_order_that_keeps_time_moving_forward():
    text = "[1/2/26, 10:00:00] Ada: a\n[1/3/26, 10:00:00] Ada: b\n[2/1/26, 10:00:00] Ada: c\n"
    # month first: Jan 2, Jan 3, Feb 1 (ascending); day first: Feb 1, Mar 1, Jan 2 (a descent)
    assert [r["sent_at"][:10] for r in parse_export(text, tz="UTC")] == ["2026-01-02", "2026-01-03", "2026-02-01"]
    assert parse_export(text, tz="UTC", dayfirst=True)[0]["sent_at"][:10] == "2026-02-01"


def test_a_zip_is_read_through_its_chat_file():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("_chat.txt", IOS)
        z.writestr("00000001-PHOTO.jpg", b"\xff")
    assert export_text(buffer.getvalue()) == IOS


def test_overlapping_exports_add_only_what_is_new_and_read_serves_since():
    store = {}
    adapter = WhatsAppExport(store=store, self_names=["Bram"])
    ref = adapter.parse_ref("ada-example")
    first = adapter.import_export(ref, IOS.encode(), tz="UTC")
    assert (first["added"], first["total"], first["previous_last"]) == (6, 6, None)
    again = adapter.import_export(ref, IOS.encode(), tz="UTC")
    assert again["added"] == 0 and again["total"] == 6
    later = adapter.import_export(ref, LATER.encode(), tz="UTC")
    assert later["added"] == 1 and later["first_added"] == "2026-03-01T18:40:00+00:00"
    assert later["previous_last"] == "2026-02-13T09:07:00+00:00"
    assert len(adapter.imports(ref)) == 3
    new = adapter.read(ref, since=later["previous_last"] and __import__("correspond.model", fromlist=["parse_time"]).parse_time("2026-02-14T00:00:00Z"))
    assert [m.text for m in new] == ["the next round"] and new[0].author.is_self
    assert store["ada-example.txt"].splitlines()[-1] == "2026-03-01T18:40:00+00:00 Bram: the next round"
    assert "[image]" in store["ada-example.txt"]


def test_same_second_duplicates_keep_distinct_ids():
    text = "[2/11/26, 12:00:00] Ada: ok\n[2/11/26, 12:00:00] Ada: ok\n"
    adapter = WhatsAppExport(store={})
    ref = adapter.parse_ref("dup")
    assert adapter.import_export(ref, text.encode(), tz="UTC")["added"] == 2
    ids = [m.id for m in adapter.read(ref)]
    assert len(set(ids)) == 2 and ids[1].endswith("-1")


def test_chat_names_are_slugs():
    with pytest.raises(InvalidRef):
        WhatsAppExport(store={}).parse_ref("Ada Example")


def test_the_tool_imports_and_reports_through_the_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPOND_DATA_DIR", str(tmp_path / "data"))
    export = tmp_path / "_chat.txt"
    export.write_text(IOS, encoding="utf-8")
    result = tools.import_export(str(export), "whatsapp:ada-example", tz="UTC")
    assert result["ok"] and result["added"] == 6, result
    assert result["transcript"] == str(tmp_path / "data" / "whatsapp" / "ada-example.txt")
    assert (tmp_path / "data" / "whatsapp" / "ada-example.txt").exists()
    read = tools.read("whatsapp:ada-example", since="2026-02-13T00:00:00Z")
    assert read["ok"] and read["count"] == 3
    refused = tools.import_export(str(export), "whatsapp:ada-example", date_order="ymd")
    assert refused["ok"] is False
    wrong = tools.import_export(str(export), "github:example/demo#1")
    assert wrong["ok"] is False and wrong["error_kind"]


def test_audience_is_the_senders_seen():
    adapter = WhatsAppExport(store={}, self_names=["Bram"])
    ref = adapter.parse_ref("ada-example")
    adapter.import_export(ref, IOS.encode(), tz="UTC")
    seen = adapter.audience(ref)
    assert seen.scope.value == "named" and seen.external is True
    assert {r.display_name for r in seen.readers} == {"Ada Example", "Bram"}


def test_a_zone_is_utc_an_offset_or_a_name_and_an_unknown_one_is_refused():
    line = "[2/11/26, 12:00:00] Ada: hi\n"
    assert parse_export(line, tz="+01:00")[0]["sent_at"] == "2026-02-11T12:00:00+01:00"
    assert parse_export(line, tz="Z")[0]["sent_at"] == "2026-02-11T12:00:00+00:00"
    from correspond.errors import CorrespondError

    with pytest.raises(CorrespondError, match="offset"):
        parse_export(line, tz="Nowhere/Atlantis")


def test_credentials_are_redacted_before_anything_is_stored():
    text = (
        "[2/11/26, 12:00:00] Ada Example: here is the login for the shared account\n"
        "[2/11/26, 12:00:05] Ada Example: Zq7#mWp2xLv9\n"
        "[2/11/26, 12:00:09] Ada Example: examplepass4821\n"
        "[2/11/26, 12:01:00] Bram: api key = sk-abcdefghijklmnopqrstu\n"
        "[2/11/26, 12:02:00] Bram: Version2.0beta is out\n"
    )
    store = {}
    adapter = WhatsAppExport(store=store)
    ref = adapter.parse_ref("ada-example")
    report = adapter.import_export(ref, text.encode(), tz="UTC")
    assert report["redacted"] == 3
    archived = "".join(store.values())
    assert "Zq7#mWp2xLv9" not in archived and "sk-abcdefghijklmnopqrstu" not in archived
    texts = [m.text for m in adapter.read(ref)]
    assert texts[1] == texts[2] == "[redacted: credential]" and texts[3].endswith("[redacted: credential]")
    assert "examplepass4821" not in archived
    assert texts[4] == "Version2.0beta is out"  # no credential mentioned nearby: kept
    kept = WhatsAppExport(store={})
    assert kept.import_export(kept.parse_ref("raw"), text.encode(), tz="UTC", redact=None)["redacted"] == 0
