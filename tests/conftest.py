import pytest


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("EDITOR_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EDITOR_LOCAL", "1")  # tests/test_auth.py turns login back on
