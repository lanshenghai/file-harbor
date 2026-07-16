# File Harbor

A small web UI for browsing SMB/SFTP shares and downloading selected files to a
directory on the **machine running this server**.

## Setup

```bash
pip install -r requirements.txt
```

SMB requires `gio`, `gvfs-smb`, `gvfs-fuse`, and `libsmbclient`. It uses one
persistent GVFS mount so concurrent downloads share the same SMB session.
Kerberos authentication still uses server-side credentials.

Create a project-local configuration file:

```bash
cp .env.example .env
```

Edit `.env` with the SFTP host, SMB domain, credentials, web server bind
settings, and UI defaults (`REMOTE_PATH`, `LOCAL_DIR`, and `WORKERS`). It is
loaded automatically when the app starts and is ignored by Git. Existing shell
environment variables take precedence over values in `.env`.

Instead of storing credentials in `.env`, set `SMB_CREDENTIALS` there to a file
containing:

```ini
username=alice
password=your-password
domain=EXAMPLE.COM
```

The default credentials file is `~/.smbcredentials`. Never commit `.env` or the
credentials file.

## Run

```bash
python3 -m app.main
```

The server listens on the address and port configured in `.env`. Command-line
arguments override those values:

```bash
python3 -m app.main --host 127.0.0.1 --port 8090
```

Open `http://127.0.0.1:8088`. When binding to a network interface, ensure your
firewall allows the selected port.

> The local directory entered in the UI is on the server host, not on the
> browser's machine.

## Run tests

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest -q
```
