import subprocess
from dataclasses import replace
from datetime import date

import pytest

from src import config, publish


DAY = date(2026, 7, 28)


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[paths]\nnews = "news"\n\n[publish]\nenabled = true\n', encoding="utf-8",
    )
    (tmp_path / "news").mkdir()
    return config.load(tmp_path / "config.toml")


class FakeGit:
    """Records git invocations. `dirty` decides whether the site changed."""

    def __init__(self, dirty=True, fail_on=None, message="fatal: no upstream"):
        self.dirty = dirty
        self.fail_on = fail_on
        self.message = message
        self.calls = []

    def __call__(self, cmd, **kwargs):
        args = [a for a in cmd if a not in ("git", "-C")]
        self.calls.append(cmd)
        verb = cmd[3] if len(cmd) > 3 else ""

        if verb == "diff":
            # git diff --cached --quiet exits 1 when there ARE changes.
            return subprocess.CompletedProcess(cmd, 1 if self.dirty else 0, "", "")
        if self.fail_on and verb == self.fail_on:
            return subprocess.CompletedProcess(cmd, 128, "", self.message)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def verbs(self):
        return [c[3] for c in self.calls if len(c) > 3]

    def find(self, verb):
        return next((c for c in self.calls if len(c) > 3 and c[3] == verb), None)

    def message_for(self, verb):
        """The -m argument of a call, so substring assertions work on the text."""
        call = self.find(verb)
        return call[call.index("-m") + 1] if call and "-m" in call else ""


def fake_export(count=1):
    calls = []

    def build(news, web, out):
        calls.append((news, web, out))
        return count

    build.calls = calls
    return build


# --- the happy path --------------------------------------------------------


def test_a_changed_site_is_committed_and_pushed(cfg):
    git = FakeGit(dirty=True)

    result = publish.publish(cfg, DAY, topic_count=26, runner=git,
                             exporter=fake_export())

    assert result.ok is True
    assert result.published is True
    assert git.verbs() == ["add", "diff", "commit", "push"]


def test_the_commit_message_names_the_day_and_topic_count(cfg):
    git = FakeGit(dirty=True)
    publish.publish(cfg, DAY, topic_count=26, runner=git, exporter=fake_export())

    assert git.message_for("commit") == "news: 2026-07-28 (26 topics)"


def test_one_topic_is_not_pluralised(cfg):
    git = FakeGit(dirty=True)
    publish.publish(cfg, DAY, topic_count=1, runner=git, exporter=fake_export())
    assert git.message_for("commit") == "news: 2026-07-28 (1 topic)"


def test_the_push_targets_the_configured_remote_and_branch(cfg):
    git = FakeGit(dirty=True)
    publish.publish(cfg, DAY, runner=git, exporter=fake_export())

    push = git.find("push")
    assert "origin" in push
    assert "HEAD:main" in push


def test_the_export_runs_before_git_touches_anything(cfg):
    git = FakeGit(dirty=True)
    build = fake_export()

    publish.publish(cfg, DAY, runner=git, exporter=build)

    assert len(build.calls) == 1
    assert git.calls          # git ran, and only after the export returned


# --- only site/ is ever staged --------------------------------------------


def test_only_the_site_directory_is_staged(cfg):
    """An unattended `git add -A` would sweep up whatever is in the working
    tree — a half-finished edit, a stray credential — and push it."""
    git = FakeGit(dirty=True)
    publish.publish(cfg, DAY, runner=git, exporter=fake_export())

    add = git.find("add")
    assert add[-1] == "site"
    assert "-A" not in add and "." not in add
    assert git.find("commit")[-1] == "site"


def test_a_site_outside_the_repository_is_refused(cfg, tmp_path):
    # dataclasses.replace, not a full Config(...) call: a positional
    # reconstruction breaks every time a config section is added.
    outside = replace(
        cfg, publish=config.PublishConfig(enabled=True, site="/tmp/elsewhere"),
    )
    git = FakeGit(dirty=True)

    result = publish.publish(outside, DAY, runner=git, exporter=fake_export())

    assert result.ok is False
    assert "outside the repository" in result.message
    assert git.calls == []


# --- nothing to do ---------------------------------------------------------


def test_an_unchanged_site_is_not_committed(cfg):
    """Otherwise every run adds an empty commit and history stops meaning anything."""
    git = FakeGit(dirty=False)

    result = publish.publish(cfg, DAY, runner=git, exporter=fake_export())

    assert result.ok is True
    assert result.published is False
    assert git.verbs() == ["add", "diff"]


def test_publishing_can_be_switched_off(cfg):
    off = replace(cfg, publish=config.PublishConfig(enabled=False))
    git = FakeGit()
    build = fake_export()

    result = publish.publish(off, DAY, runner=git, exporter=build)

    assert result.ok is True
    assert result.published is False
    assert git.calls == []
    assert build.calls == []      # not even the export runs


# --- failures never raise --------------------------------------------------


@pytest.mark.parametrize("verb", ["add", "commit", "push"])
def test_a_git_failure_is_reported_not_raised(cfg, verb):
    """The digest is already written; losing GitHub is not worth failing over."""
    git = FakeGit(dirty=True, fail_on=verb, message="fatal: could not read from remote")

    result = publish.publish(cfg, DAY, runner=git, exporter=fake_export())

    assert result.ok is False
    assert result.published is False
    assert "could not read from remote" in result.message


def test_an_export_failure_stops_before_git(cfg):
    def boom(news, web, out):
        raise RuntimeError("target is not ours")

    git = FakeGit(dirty=True)
    result = publish.publish(cfg, DAY, runner=git, exporter=boom)

    assert result.ok is False
    assert "target is not ours" in result.message
    assert git.calls == []


def test_git_stderr_is_truncated_rather_than_dumped(cfg):
    git = FakeGit(dirty=True, fail_on="push", message="x" * 5000)
    result = publish.publish(cfg, DAY, runner=git, exporter=fake_export())
    assert len(result.message) < 600
