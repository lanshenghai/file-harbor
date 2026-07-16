# File Harbor

A small web UI for browsing SMB/SFTP shares and downloading selected files to a
directory on the **machine running this server**.

## Setup

```bash
pip install -r requirements.txt
./install_samba_client.sh
```

`install_samba_client.sh` installs the native Samba client under `.tools/` without sudo.
SMB uses a private Kerberos cache created from server-side credentials.

Provide credentials through environment variables:

```bash
export SMB_USER="alice"
export SMB_PASS="your-password"
export SMB_DOMAIN="EXAMPLE.COM"
export SFTP_HOST="sftp.example.com"
```

Alternatively, set `SMB_CREDENTIALS` to a file containing:

```ini
username=alice
password=your-password
domain=EXAMPLE.COM
```

The default credentials file is `~/.smbcredentials`. Do not commit that file.

## Run

```bash
python3 -m app.main
```

The server listens on `0.0.0.0:8088` by default. Override it with command-line
arguments or environment variables:

```bash
python3 -m app.main --host 127.0.0.1 --port 8090
FILE_HARBOR_HOST=127.0.0.1 FILE_HARBOR_PORT=8090 python3 -m app.main
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
