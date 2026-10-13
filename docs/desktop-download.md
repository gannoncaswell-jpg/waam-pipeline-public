# Desktop release setup

```sh
python -m pip install -e '.[release]'
python -m waam_pipeline.desktop_release --help
```

For the service layout in `deploy/waam-workbench.service`:

```sh
/srv/waam/venv/bin/pip install 'cryptography>=50.0'
```

Set `WAAM_DOWNLOADS_DIR` to the release storage directory. Publish a platform-specific archive and its Ed25519 signature using the command's required arguments. The server exposes verified releases in Settings > Desktop app.
