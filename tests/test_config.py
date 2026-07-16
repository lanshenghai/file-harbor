import os

from app import main


def test_load_project_env_loads_values_without_overriding_environment(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SFTP_HOST=sftp.from-file.example\nSMB_DOMAIN=FILE.EXAMPLE\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SFTP_HOST", raising=False)
    monkeypatch.setenv("SMB_DOMAIN", "ENV.EXAMPLE")

    main.load_project_env(env_file)

    assert os.environ["SFTP_HOST"] == "sftp.from-file.example"
    assert os.environ["SMB_DOMAIN"] == "ENV.EXAMPLE"
