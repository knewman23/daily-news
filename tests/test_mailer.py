import subprocess
from datetime import date

import pytest

from src import config, mailer
from src.records import Stats


DAY = date(2026, 7, 28)
HEADLINES = [
    "Iran launches ballistic missiles at US forces in the Middle East",
    "Senate passes the spending bill",
    "Great Salt Lake nears total loss",
]

CONFIG = """\
[paths]
news = "news"

[email]
enabled = true
to = "krys.newman@gmail.com"
sender = "krys.newman@gmail.com"
keychain_service = "daily-news-smtp"
keychain_account = "krys.newman@gmail.com"
site_url = "https://knewman23.github.io/daily-news/"
"""


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG, encoding="utf-8")
    return config.load(tmp_path / "config.toml")


def keychain(password="app-password", found=True):
    def runner(cmd, **kwargs):
        if found:
            return subprocess.CompletedProcess(cmd, 0, password + "\n", "")
        return subprocess.CompletedProcess(cmd, 44, "", "not found")
    return runner


class Transport:
    def __init__(self, raises=None):
        self.raises = raises
        self.sent = []

    def __call__(self, message, host, port, _unused):
        if self.raises:
            raise self.raises
        self.sent.append({"message": message, "host": host, "port": port})


# --- the message -----------------------------------------------------------


def test_the_subject_names_the_count_and_the_day():
    subject, _ = mailer.build_message(DAY, HEADLINES, Stats(post_count=33))
    assert subject == "Daily News — 3 topics for July 28"


def test_one_topic_is_not_pluralised():
    subject, _ = mailer.build_message(DAY, ["Only one thing happened"], Stats())
    assert "1 topic for" in subject


def test_a_quiet_day_says_so():
    subject, body = mailer.build_message(DAY, [], Stats())
    assert subject == "Daily News — no news for July 28"
    assert "0 topics" in body


def test_the_body_carries_every_headline():
    """Worth reading on a lock screen without opening anything."""
    _, body = mailer.build_message(DAY, HEADLINES, Stats(post_count=33, transcribed_count=32))

    for headline in HEADLINES:
        assert headline in body
    assert "33 posts" in body
    assert "32 with usable text" in body


def test_a_long_day_is_truncated_with_a_count():
    many = [f"Topic number {i}" for i in range(60)]
    _, body = mailer.build_message(DAY, many, Stats())

    assert "Topic number 0" in body
    assert "Topic number 59" not in body
    assert f"and {60 - mailer.MAX_HEADLINES} more" in body


def test_the_site_link_is_included_when_configured():
    _, body = mailer.build_message(DAY, HEADLINES, Stats(),
                                   site_url="https://example.test/")
    assert "https://example.test/" in body


def test_no_link_line_when_no_site_is_configured():
    _, body = mailer.build_message(DAY, HEADLINES, Stats())
    assert "Read it all" not in body


def test_failures_are_surfaced_in_the_subject_and_body():
    subject, body = mailer.build_message(
        DAY, HEADLINES, Stats(),
        failures=["fetch total.hipocrisy: Profile does not exist."],
    )
    assert "(incomplete)" in subject
    assert "total.hipocrisy" in body


# --- the keychain ----------------------------------------------------------


def test_the_password_comes_from_the_keychain(cfg):
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "secret\n", "")

    transport = Transport()
    result = mailer.send(cfg, "s", "b", transport=transport, runner=runner)

    assert result.sent is True
    assert calls[0][:2] == ["security", "find-generic-password"]
    assert "daily-news-smtp" in calls[0]
    assert "krys.newman@gmail.com" in calls[0]


def test_a_missing_keychain_item_explains_how_to_add_it(cfg):
    transport = Transport()
    result = mailer.send(cfg, "s", "b", transport=transport, runner=keychain(found=False))

    assert result.ok is False
    assert result.sent is False
    assert "add-generic-password" in result.message
    assert transport.sent == []


def test_an_empty_keychain_item_is_an_error(cfg):
    transport = Transport()
    result = mailer.send(cfg, "s", "b", transport=transport, runner=keychain(password=""))

    assert result.ok is False
    assert transport.sent == []


def test_the_password_never_appears_in_the_result(cfg):
    result = mailer.send(cfg, "s", "b", transport=Transport(),
                         runner=keychain(password="hunter2"))
    assert "hunter2" not in result.message


# --- sending ---------------------------------------------------------------


def test_a_sent_message_carries_the_right_headers(cfg):
    transport = Transport()
    mailer.send(cfg, "Daily News — 3 topics", "body text",
                transport=transport, runner=keychain())

    message = transport.sent[0]["message"]
    assert message["To"] == "krys.newman@gmail.com"
    assert message["From"] == "krys.newman@gmail.com"
    assert message["Subject"] == "Daily News — 3 topics"
    assert "body text" in message.get_content()


def test_the_configured_smtp_host_and_port_are_used(cfg):
    transport = Transport()
    mailer.send(cfg, "s", "b", transport=transport, runner=keychain())

    assert transport.sent[0]["host"] == "smtp.gmail.com"
    assert transport.sent[0]["port"] == 587


def test_a_transport_failure_is_reported_not_raised(cfg):
    """A run that produced a digest and could not send the email has still
    succeeded at the part that matters."""
    result = mailer.send(cfg, "s", "b",
                         transport=Transport(raises=OSError("connection refused")),
                         runner=keychain())

    assert result.ok is False
    assert result.sent is False
    assert "connection refused" in result.message


def test_email_can_be_switched_off(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[paths]\nnews = "news"\n\n[email]\nenabled = false\n', encoding="utf-8",
    )
    off = config.load(tmp_path / "config.toml")
    transport = Transport()

    result = mailer.send(off, "s", "b", transport=transport, runner=keychain())

    assert result.ok is True
    assert result.sent is False
    assert transport.sent == []


def test_a_missing_to_address_is_refused(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[paths]\nnews = "news"\n\n[email]\nenabled = true\n', encoding="utf-8",
    )
    broken = config.load(tmp_path / "config.toml")
    transport = Transport()

    result = mailer.send(broken, "s", "b", transport=transport, runner=keychain())

    assert result.ok is False
    assert "to address" in result.message
    assert transport.sent == []


def test_a_grouped_app_password_is_normalised(cfg):
    """Google shows app passwords as four groups of four and people paste them
    verbatim, but SMTP AUTH wants the 16 characters alone."""
    seen = {}

    def transport(message, host, port, _unused):
        seen["ok"] = True

    captured = {}

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "abcd efgh ijkl mnop\n", "")

    password = mailer.keychain_password("svc", "acct", runner=runner)
    assert password == "abcdefghijklmnop"


def test_a_whitespace_only_keychain_item_is_an_error():
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "   \n", "")

    with pytest.raises(mailer.MailError):
        mailer.keychain_password("svc", "acct", runner=runner)
