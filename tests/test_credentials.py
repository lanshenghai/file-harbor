from app.credentials import load_saved_credentials


def test_saved_credentials_do_not_assume_a_domain(monkeypatch, tmp_path):
    credentials = tmp_path / "credentials"
    credentials.write_text("username=alice\npassword=secret\n", encoding="utf-8")
    monkeypatch.delenv("SMB_DOMAIN", raising=False)

    saved = load_saved_credentials(credentials)

    assert saved is not None
    assert saved.domain == ""
