import json
from datetime import datetime, timedelta, timezone

import pytest

from src import sources


SEED = {
    "version": 2,
    "sources": [
        {"handle": "total.hipocrisy", "enabled": True, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
        {"handle": "aaronparnas", "enabled": True, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
        {"handle": "cancel.ian.carroll", "enabled": True, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
        {"handle": "carolinegleich", "enabled": True, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
        {"handle": "hunteralexanderhowell", "enabled": True, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
        {"handle": "oafnation_actual", "enabled": True, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
    ],
}


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "sources.json"
    p.write_text(json.dumps(SEED, indent=2) + "\n", encoding="utf-8")
    return p


def ok_lookup(handle):
    """Stub profile lookup that succeeds."""


def bad_lookup(handle):
    raise RuntimeError("profile not found")


# --- normalize -------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "foo.bar",
    "@foo.bar",
    "  @Foo.Bar  ",
    "https://instagram.com/foo.bar",
    "https://www.instagram.com/foo.bar/",
    "instagram.com/foo.bar?hl=en",
])
def test_normalize_accepts_equivalent_forms(raw):
    assert sources.normalize(raw) == "foo.bar"


@pytest.mark.parametrize("raw", ["", "@", "two words", "foo/bar", "a" * 40])
def test_normalize_rejects_invalid(raw):
    with pytest.raises(ValueError):
        sources.normalize(raw)


@pytest.mark.parametrize("raw", ["total.hipocrisy", "cancel.ian.carroll", "oafnation_actual"])
def test_normalize_preserves_dots_and_underscores(raw):
    assert sources.normalize(raw) == raw


# --- load / enabled_sources ------------------------------------------------


def test_load_returns_every_seed_source(path):
    loaded = sources.load(path)
    assert len(loaded) == 6
    assert loaded[0].handle == "total.hipocrisy"
    assert loaded[0].last_pull_at is None
    assert loaded[0].last_seen is None


def test_enabled_sources_returns_records_not_handles(path):
    enabled = sources.enabled_sources(path)
    assert len(enabled) == 6
    assert all(isinstance(s, sources.Source) for s in enabled)


# --- add -------------------------------------------------------------------


def test_add_appends_with_null_watermarks(path):
    rec = sources.add(path, "@NewHandle", lookup=ok_lookup, today="2026-08-01")
    assert rec.handle == "newhandle"
    assert rec.enabled is True
    assert rec.added == "2026-08-01"
    assert rec.last_pull_at is None
    assert rec.last_seen is None
    assert [s.handle for s in sources.load(path)][-1] == "newhandle"


def test_add_leaves_file_untouched_when_lookup_fails(path):
    before = path.read_bytes()
    with pytest.raises(sources.LookupFailed) as exc:
        sources.add(path, "ghostaccount", lookup=bad_lookup)
    assert "profile not found" in str(exc.value)
    assert path.read_bytes() == before


def test_add_rejects_duplicate_after_normalization(path):
    with pytest.raises(sources.DuplicateHandle):
        sources.add(path, "@Aaronparnas", lookup=ok_lookup)
    assert len(sources.load(path)) == 6


def test_add_rejects_invalid_handle_without_lookup(path):
    calls = []
    with pytest.raises(ValueError):
        sources.add(path, "two words", lookup=lambda h: calls.append(h))
    assert calls == []


# --- set_enabled / remove --------------------------------------------------


def test_disable_hides_from_enabled_but_keeps_record(path):
    sources.set_enabled(path, "carolinegleich", False)
    assert "carolinegleich" not in [s.handle for s in sources.enabled_sources(path)]
    assert "carolinegleich" in [s.handle for s in sources.load(path)]


def test_remove_drops_record_entirely(path):
    sources.remove(path, "carolinegleich")
    assert "carolinegleich" not in [s.handle for s in sources.load(path)]
    assert len(sources.load(path)) == 5


@pytest.mark.parametrize("call", [
    lambda p: sources.remove(p, "nobody"),
    lambda p: sources.set_enabled(p, "nobody", False),
    lambda p: sources.advance_watermark(p, "nobody", "2026-07-28T11:00:00+00:00"),
    lambda p: sources.stamp_last_seen(p, "nobody", "2026-07-28"),
])
def test_unknown_handle_raises(path, call):
    with pytest.raises(sources.UnknownHandle):
        call(path)


# --- watermark and last_seen ----------------------------------------------


def test_advance_watermark_touches_only_one_record(path):
    when = datetime(2026, 7, 28, 11, 0, tzinfo=timezone.utc)
    sources.advance_watermark(path, "aaronparnas", when)

    by_handle = {s.handle: s for s in sources.load(path)}
    assert by_handle["aaronparnas"].last_pull_at == "2026-07-28T11:00:00+00:00"
    others = [s for h, s in by_handle.items() if h != "aaronparnas"]
    assert len(others) == 5
    assert all(s.last_pull_at is None for s in others)


def test_stamp_last_seen_touches_only_one_record(path):
    sources.stamp_last_seen(path, "aaronparnas", "2026-07-28")

    by_handle = {s.handle: s for s in sources.load(path)}
    assert by_handle["aaronparnas"].last_seen == "2026-07-28"
    assert by_handle["aaronparnas"].last_pull_at is None
    assert all(s.last_seen is None for h, s in by_handle.items() if h != "aaronparnas")


def test_watermark_never_moves_backwards(path):
    later = datetime(2026, 7, 28, 11, 0, tzinfo=timezone.utc)
    earlier = later - timedelta(days=3)

    sources.advance_watermark(path, "aaronparnas", later)
    sources.advance_watermark(path, "aaronparnas", earlier)

    by_handle = {s.handle: s for s in sources.load(path)}
    assert by_handle["aaronparnas"].last_pull_at == "2026-07-28T11:00:00+00:00"


def test_watermark_accepts_iso_string(path):
    sources.advance_watermark(path, "aaronparnas", "2026-07-28T11:00:00+00:00")
    by_handle = {s.handle: s for s in sources.load(path)}
    assert by_handle["aaronparnas"].last_pull_at == "2026-07-28T11:00:00+00:00"


def test_watermark_rejects_naive_datetime(path):
    with pytest.raises(ValueError):
        sources.advance_watermark(path, "aaronparnas", datetime(2026, 7, 28, 11, 0))


# --- file safety -----------------------------------------------------------


def test_corrupt_json_raises_and_never_returns_empty(tmp_path):
    p = tmp_path / "sources.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(sources.SourcesFileError) as exc:
        sources.load(p)
    assert str(p) in str(exc.value)


def test_missing_file_raises(tmp_path):
    with pytest.raises(sources.SourcesFileError):
        sources.load(tmp_path / "absent.json")


@pytest.mark.parametrize("payload", [
    {"version": 2},
    {"version": 2, "sources": "not-a-list"},
    {"version": 2, "sources": [{"enabled": True}]},
    [],
])
def test_wrong_shape_raises(tmp_path, payload):
    p = tmp_path / "sources.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(sources.SourcesFileError):
        sources.load(p)


def test_mutations_leave_no_temp_file_and_stay_parseable(path):
    sources.add(path, "newhandle", lookup=ok_lookup, today="2026-08-01")
    sources.set_enabled(path, "newhandle", False)
    sources.advance_watermark(path, "newhandle", "2026-08-01T11:00:00+00:00")
    sources.stamp_last_seen(path, "newhandle", "2026-08-01")
    sources.remove(path, "newhandle")

    assert list(path.parent.glob("*.tmp")) == []
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2
    assert len(sources.load(path)) == 6


def test_mutation_preserves_field_order_and_unknown_keys(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["sources"][0]["note"] = "keep me"
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

    sources.stamp_last_seen(path, "aaronparnas", "2026-07-28")

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["sources"][0]["note"] == "keep me"
