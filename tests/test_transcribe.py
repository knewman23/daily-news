import json
import subprocess

import pytest

from src import transcribe
from src.config import TranscribeConfig


CFG = TranscribeConfig(model="small", compute_type="int8", min_words=10)

SPEECH = (
    "The Senate passed the spending bill today after a long weekend "
    "of negotiation between the two parties."
)


class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeModel:
    """Stands in for faster_whisper.WhisperModel."""

    def __init__(self, text=SPEECH):
        self.text = text
        self.calls = []

    def transcribe(self, path, **kwargs):
        self.calls.append({"path": path, **kwargs})
        return ([FakeSegment(self.text)], {"language": "en"})


class CountingFactory:
    """Records how many times a model was constructed."""

    def __init__(self, model=None):
        self.model = model or FakeModel()
        self.count = 0

    def __call__(self, cfg):
        self.count += 1
        return self.model


class FakeRunner:
    """Stands in for subprocess.run for ffprobe / ffmpeg."""

    def __init__(self, has_audio=True, fail_extract=False):
        self.has_audio = has_audio
        self.fail_extract = fail_extract
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        program = cmd[0]

        if program == "ffprobe":
            out = "audio\n" if self.has_audio else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        if self.fail_extract:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ffmpeg boom")

        # Pretend ffmpeg wrote the wav so downstream code finds a real file.
        wav = cmd[-1]
        with open(wav, "wb") as handle:
            handle.write(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def programs(self):
        return [c[0] for c in self.calls]


def make_post(raw_dir, handle="aaronparnas", shortcode="AAA", sidecar=True):
    raw_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{handle}_{shortcode}"
    (raw_dir / f"{stem}.mp4").write_bytes(b"fake-mp4")
    if sidecar:
        (raw_dir / f"{stem}.json").write_text(json.dumps({
            "handle": handle,
            "shortcode": shortcode,
            "kind": "video",
            "caption": "Breaking news",
            "permalink": f"https://www.instagram.com/p/{shortcode}/",
            "posted_at": "2026-07-28T09:00:00+00:00",
        }), encoding="utf-8")
    return raw_dir / f"{stem}.mp4"


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "raw", tmp_path / "out"


# --- the happy path --------------------------------------------------------


def test_transcribes_a_post_and_writes_the_text(dirs):
    raw, out = dirs
    make_post(raw)
    runner = FakeRunner()
    factory = CountingFactory()

    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG, model_factory=factory, runner=runner,
    )

    assert len(transcripts) == 1
    assert transcripts[0].handle == "aaronparnas"
    assert transcripts[0].shortcode == "AAA"
    assert transcripts[0].text == SPEECH
    assert transcripts[0].caption == "Breaking news"
    assert transcripts[0].permalink.endswith("/AAA/")
    assert (out / "aaronparnas_AAA.txt").read_text(encoding="utf-8").strip() == SPEECH
    assert stats.post_count == 1
    assert stats.transcribed_count == 1
    assert stats.incomplete is False


def test_uses_16k_mono_pcm_and_nostdin(dirs):
    raw, out = dirs
    make_post(raw)
    runner = FakeRunner()

    transcribe.transcribe_day(raw, out, CFG, model_factory=CountingFactory(), runner=runner)

    ffmpeg = next(c for c in runner.calls if c[0] == "ffmpeg")
    assert "-nostdin" in ffmpeg
    assert "-ac" in ffmpeg and "1" in ffmpeg
    assert "-ar" in ffmpeg and "16000" in ffmpeg
    assert "pcm_s16le" in ffmpeg


def test_enables_the_vad_filter(dirs):
    raw, out = dirs
    make_post(raw)
    factory = CountingFactory()

    transcribe.transcribe_day(raw, out, CFG, model_factory=factory, runner=FakeRunner())

    assert factory.model.calls[0]["vad_filter"] is True


def test_intermediate_wav_is_cleaned_up(dirs):
    raw, out = dirs
    make_post(raw)

    transcribe.transcribe_day(raw, out, CFG, model_factory=CountingFactory(), runner=FakeRunner())

    assert list(out.glob("*.wav")) == []
    assert list(raw.glob("*.wav")) == []


# --- resumability ----------------------------------------------------------


def test_existing_transcript_is_reused_without_touching_ffmpeg_or_the_model(dirs):
    raw, out = dirs
    make_post(raw)
    out.mkdir(parents=True)
    (out / "aaronparnas_AAA.txt").write_text("already done, plenty of words here now ok", encoding="utf-8")

    runner = FakeRunner()
    factory = CountingFactory()

    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG, model_factory=factory, runner=runner,
    )

    assert runner.calls == []
    assert factory.count == 0
    assert len(transcripts) == 1
    assert transcripts[0].text.startswith("already done")
    assert stats.transcribed_count == 1


def test_model_is_constructed_once_per_run_not_once_per_file(dirs):
    raw, out = dirs
    for code in ("AAA", "BBB", "CCC"):
        make_post(raw, shortcode=code)
    factory = CountingFactory()

    transcripts, _ = transcribe.transcribe_day(
        raw, out, CFG, model_factory=factory, runner=FakeRunner(),
    )

    assert len(transcripts) == 3
    assert factory.count == 1


def test_model_is_not_constructed_when_there_is_nothing_to_do(dirs):
    raw, out = dirs
    factory = CountingFactory()

    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG, model_factory=factory, runner=FakeRunner(),
    )

    assert transcripts == []
    assert factory.count == 0
    assert stats.incomplete is False


# --- skips and failures ---------------------------------------------------


def test_video_with_no_audio_track_is_counted_but_not_transcribed(dirs):
    raw, out = dirs
    make_post(raw)
    runner = FakeRunner(has_audio=False)
    factory = CountingFactory()

    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG, model_factory=factory, runner=runner,
    )

    assert transcripts == []
    assert stats.post_count == 1
    assert stats.transcribed_count == 0
    assert "ffmpeg" not in runner.programs()
    assert factory.count == 0


def test_transcript_below_the_word_floor_is_skipped(dirs):
    raw, out = dirs
    make_post(raw)
    factory = CountingFactory(FakeModel("uh, hey"))

    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG, model_factory=factory, runner=FakeRunner(),
    )

    assert transcripts == []
    assert stats.transcribed_count == 0
    assert not list(out.glob("*.txt"))


def test_empty_transcription_is_skipped(dirs):
    raw, out = dirs
    make_post(raw)
    factory = CountingFactory(FakeModel("   "))

    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG, model_factory=factory, runner=FakeRunner(),
    )
    assert transcripts == []
    assert stats.transcribed_count == 0


def test_one_failing_file_does_not_stop_the_others(dirs):
    raw, out = dirs
    make_post(raw, shortcode="AAA")
    make_post(raw, shortcode="BBB")

    class OneBadModel(FakeModel):
        def transcribe(self, path, **kwargs):
            if "AAA" in path:
                raise RuntimeError("decode failed")
            return super().transcribe(path, **kwargs)

    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG, model_factory=CountingFactory(OneBadModel()), runner=FakeRunner(),
    )

    assert [t.shortcode for t in transcripts] == ["BBB"]
    assert stats.post_count == 2
    assert stats.transcribed_count == 1
    assert stats.incomplete is True


def test_ffmpeg_failure_marks_the_day_incomplete(dirs):
    raw, out = dirs
    make_post(raw)

    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG,
        model_factory=CountingFactory(), runner=FakeRunner(fail_extract=True),
    )

    assert transcripts == []
    assert stats.incomplete is True
    assert any("ffmpeg" in note for note in stats.notes)


def test_media_with_no_sidecar_is_not_treated_as_a_post(dirs):
    """The sidecar defines the post. Guessing a handle out of the filename
    risked attributing it to the wrong account, since both handles and
    shortcodes may contain underscores."""
    from src import posts

    raw, out = dirs
    make_post(raw, handle="oafnation_actual", shortcode="XYZ", sidecar=False)

    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG, model_factory=CountingFactory(), runner=FakeRunner(),
    )

    assert transcripts == []
    assert stats.post_count == 0
    # It is still visible rather than silently dropped.
    assert [p.name for p in posts.orphans(raw)] == ["oafnation_actual_XYZ.mp4"]


def test_a_video_pruned_before_transcription_is_flagged(dirs):
    """Nothing can recover the audio, so the day must say it is incomplete
    rather than quietly report a thinner day."""
    raw, out = dirs
    make_post(raw)
    (raw / "aaronparnas_AAA.mp4").unlink()

    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG, model_factory=CountingFactory(), runner=FakeRunner(),
    )

    assert transcripts == []
    assert stats.post_count == 1
    assert stats.incomplete is True
    assert any("pruned" in note for note in stats.notes)


def test_an_existing_transcript_survives_the_media_being_pruned(dirs):
    """The whole point of pruning: a day still summarizes with media gone."""
    raw, out = dirs
    make_post(raw)
    out.mkdir(parents=True)
    (out / "aaronparnas_AAA.txt").write_text("plenty of words here for the floor", encoding="utf-8")
    (raw / "aaronparnas_AAA.mp4").unlink()

    runner = FakeRunner()
    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG, model_factory=CountingFactory(), runner=runner,
    )

    assert len(transcripts) == 1
    assert transcripts[0].handle == "aaronparnas"
    assert stats.incomplete is False
    assert runner.calls == []


def test_transcripts_come_back_in_a_stable_order(dirs):
    raw, out = dirs
    for code in ("CCC", "AAA", "BBB"):
        make_post(raw, shortcode=code)

    transcripts, _ = transcribe.transcribe_day(
        raw, out, CFG, model_factory=CountingFactory(), runner=FakeRunner(),
    )
    assert [t.shortcode for t in transcripts] == ["AAA", "BBB", "CCC"]


def test_missing_raw_directory_is_an_empty_day_not_a_crash(tmp_path):
    transcripts, stats = transcribe.transcribe_day(
        tmp_path / "absent", tmp_path / "out", CFG,
        model_factory=CountingFactory(), runner=FakeRunner(),
    )
    assert transcripts == []
    assert stats.post_count == 0
    assert stats.incomplete is False


def test_missing_ffmpeg_fails_fast_with_install_instructions():
    with pytest.raises(transcribe.ToolMissing) as exc:
        transcribe.require_tools(which=lambda name: None)
    assert "brew install ffmpeg" in str(exc.value)


def test_require_tools_passes_when_both_binaries_exist():
    transcribe.require_tools(which=lambda name: f"/opt/homebrew/bin/{name}")


# --- settled with nothing usable -------------------------------------------


def test_a_video_with_no_audio_is_recorded_as_settled(dirs):
    """Otherwise it looks un-extracted forever: whisper retries it every run and
    prune keeps its media for a transcript that cannot exist."""
    from src import posts

    raw, out = dirs
    make_post(raw)

    transcribe.transcribe_day(raw, out, CFG, model_factory=CountingFactory(),
                             runner=FakeRunner(has_audio=False))

    marker = out / f"aaronparnas_AAA{posts.SETTLED_SUFFIX}"
    assert marker.is_file()
    assert "no audio" in marker.read_text(encoding="utf-8")


def test_a_below_floor_transcript_is_recorded_as_settled(dirs):
    from src import posts

    raw, out = dirs
    make_post(raw)

    transcribe.transcribe_day(raw, out, CFG,
                             model_factory=CountingFactory(FakeModel("uh, hey")),
                             runner=FakeRunner())

    assert (out / f"aaronparnas_AAA{posts.SETTLED_SUFFIX}").is_file()


def test_a_settled_post_is_not_transcribed_again(dirs):
    """Re-running whisper on a silent video produces the same nothing."""
    from src import posts

    raw, out = dirs
    make_post(raw)
    out.mkdir(parents=True)
    (out / f"aaronparnas_AAA{posts.SETTLED_SUFFIX}").write_text("no audio\n", encoding="utf-8")

    runner = FakeRunner()
    factory = CountingFactory()
    transcripts, stats = transcribe.transcribe_day(
        raw, out, CFG, model_factory=factory, runner=runner)

    assert runner.calls == []
    assert factory.count == 0
    assert transcripts == []
    assert stats.incomplete is False
