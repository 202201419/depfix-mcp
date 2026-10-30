import pytest


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Tests must never touch the real ~/.config/depfix key or depend on your git identity."""
    monkeypatch.setenv("DEPFIX_HOME", str(tmp_path_factory.mktemp("depfix-home")))
    for k, v in {
        "GIT_AUTHOR_NAME": "tester", "GIT_AUTHOR_EMAIL": "tester@example.invalid",
        "GIT_COMMITTER_NAME": "tester", "GIT_COMMITTER_EMAIL": "tester@example.invalid",
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
