# Service setup

The service templates use user `waam`, checkout `/srv/waam/waam-pipeline`, and interpreter `/srv/waam/venv/bin/python`. Adjust these paths for your host and install dependencies there.

Install `waam-workbench.service` under `/etc/systemd/system/`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now waam-workbench
```

The service binds localhost and requires a token. Configure HTTPS in front of it for remote access. Keep local authentication configuration and account files out of Git.
