import os
from pathlib import Path

import pytest

from cv_maker import paths
from cv_maker.settings import get_secret_key


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # no ./data here unless a test creates one


def test_default_is_per_user_folder_outside_the_project(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    path, source = paths.resolve_data_dir()
    assert (path, source) == (tmp_path / "AppData" / "Local" / "CVTailor", "default")


@pytest.mark.parametrize("platform,expected", [
    ("darwin", Path("Library") / "Application Support" / "CVTailor"),
    ("linux", Path(".local") / "share" / "cv-tailor"),
])
def test_default_folder_per_os(monkeypatch, tmp_path, platform, expected):
    monkeypatch.setattr(paths.sys, "platform", platform)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert paths.user_data_dir() == tmp_path / expected


def test_data_dir_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "mine"))
    assert paths.resolve_data_dir() == (tmp_path / "mine", "DATA_DIR")


def test_existing_legacy_folder_keeps_working(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "cv_maker.sqlite").write_bytes(b"")
    assert paths.resolve_data_dir() == (Path("data"), "legacy")


def test_empty_legacy_folder_is_not_used(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(paths, "user_data_dir", lambda: tmp_path / "appdata")
    assert paths.resolve_data_dir() == (tmp_path / "appdata", "default")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_private_permissions(tmp_path):
    folder = tmp_path / "d"
    paths.make_private_dir(folder)
    assert oct(folder.stat().st_mode & 0o777) == "0o700"


def test_weak_secret_key_from_env_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_KEY", "cv-maker-dev-secret")
    key = get_secret_key(tmp_path)
    assert key != "cv-maker-dev-secret" and len(key) == 64
    assert get_secret_key(tmp_path) == key  # stable per install
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    assert get_secret_key(tmp_path) == "x" * 40


def test_warns_when_data_folder_would_be_committed(tmp_path, caplog):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    if subprocess.run(["git", "init", "-q", str(repo)], capture_output=True).returncode != 0:
        pytest.skip("git not available")
    exposed = repo / "mydata"
    exposed.mkdir()
    assert paths.warn_if_committable(exposed) is True
    assert "NOT ignored" in caplog.text
    (repo / ".gitignore").write_text("mydata/\n")
    assert paths.warn_if_committable(exposed) is False
