import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from waam_pipeline import accounts, oidc, server

root = Path(sys.argv[1]).resolve()
assert root.is_dir()
oidc._auth_config_file = lambda: root / ".waam-auth.json"
accounts._accounts_file = lambda: root / ".waam-accounts.json"

if len(sys.argv) > 2 and sys.argv[2] == "reset":
    original_hash = accounts.hash_password
    def observed_hash(password):
        result = original_hash(password)
        (root / "reset-ready").write_text("ready")
        return result
    accounts.hash_password = observed_hash
    raise SystemExit(accounts._main(["reset", "worker@example.com",
                                     "--password", "a different strong phrase"]))

server.ACCOUNTS = accounts.Store()
server.SAVES_DIR = root / "saves"
server.ACCESS_LOG = root / "access.log"


server.ACTIVITY_LOG = root / "activity.log"
server._tokens_file = lambda: root / ".waam-tokens.json"
server._token_file = lambda: root / ".waam-token"
original_server = server.ThreadingHTTPServer
def ready_server(*args, **kwargs):
    httpd = original_server(*args, **kwargs)


    pending = root / "ready.json.tmp"
    pending.write_text(json.dumps({"port": httpd.server_port}))
    pending.replace(root / "ready.json")
    return httpd
server.ThreadingHTTPServer = ready_server
server.serve(port=0, show_cell=False, token="isolated-test-token", require_token=True)
