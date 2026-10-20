"""Tests for telegram_alert.load_env_vars (.env parsing for Telegram alerts).

telegram_alert.py is copied into a temp directory so the tests control the
.env files its loader searches for (module dir, parent dir) without touching
the real repository files.
"""

import importlib.util
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOKENS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


@pytest.fixture()
def alert_mod(tmp_path, monkeypatch):
    for k in TOKENS:
        monkeypatch.delenv(k, raising=False)
    mod_path = tmp_path / "telegram_alert.py"
    shutil.copy(REPO / "telegram_alert.py", mod_path)
    spec = importlib.util.spec_from_file_location("telegram_alert_under_test",
                                                  mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, tmp_path


def test_no_env_file_no_env_vars_returns_empty(alert_mod):
    mod, _ = alert_mod
    assert mod.load_env_vars() == {}


def test_parses_env_file_ignoring_comments_and_quotes(alert_mod):
    mod, tmp_path = alert_mod
    (tmp_path / ".env").write_text(
        "# telegram config\n"
        "TELEGRAM_BOT_TOKEN=\"123:abc\"\n"
        "TELEGRAM_CHAT_ID='-100123'\n"
        "OTHER=value\n"
        "MALFORMED LINE WITHOUT EQUALS\n")
    vals = mod.load_env_vars()
    assert vals["TELEGRAM_BOT_TOKEN"] == "123:abc"
    assert vals["TELEGRAM_CHAT_ID"] == "-100123"
    assert vals["OTHER"] == "value"


def test_system_environment_overrides_file(alert_mod, monkeypatch):
    mod, tmp_path = alert_mod
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=file-token\nTELEGRAM_CHAT_ID=file-chat\n")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    vals = mod.load_env_vars()
    assert vals["TELEGRAM_BOT_TOKEN"] == "env-token"
    assert vals["TELEGRAM_CHAT_ID"] == "file-chat"
