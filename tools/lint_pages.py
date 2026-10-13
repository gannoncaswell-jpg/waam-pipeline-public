from __future__ import annotations

import json
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ScriptCollector(HTMLParser):


    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts: list[dict] = []
        self.handlers: list[str] = []
        self._active: dict | None = None
        self._inside_script = False

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        for name, value in attributes:
            if name.startswith("on") and value is not None:
                self.handlers.append(value)
        if tag != "script":
            return
        if self._inside_script:
            raise ValueError("Nested script element in rendered page")
        self._inside_script = True
        if "src" in attrs:
            raise ValueError("External script must be added explicitly to the lint inventory")
        kind = (attrs.get("type") or "").lower().split(";", 1)[0].strip()
        if kind in {"application/json", "application/ld+json", "importmap", "speculationrules"}:
            return
        if kind not in {"", "text/javascript", "application/javascript"}:
            raise ValueError(f"Add lint handling for script type {kind!r}")
        self._active = {"line": self.getpos()[0], "code": ""}

    def handle_data(self, data):
        if self._active is not None:
            self._active["code"] += data

    def handle_endtag(self, tag):
        if tag == "script":
            if self._active is not None:
                self.scripts.append(self._active)
            self._active = None
            self._inside_script = False

    def close(self):
        super().close()
        if self._inside_script:
            raise ValueError("Unclosed script element in rendered page")


def remove_vendor(markup: str, vendor: str) -> str:

    escaped = vendor.replace("</script", "<\\/script")
    if markup.count(escaped) != 1:
        raise ValueError("Expected exactly one unchanged Three.js vendor payload")
    return markup.replace(escaped, "/* Unmodified vendored Three.js omitted from lint. */", 1)


def collect_page(name: str, markup: str) -> dict:
    parser = ScriptCollector()
    parser.feed(markup)
    parser.close()
    if not parser.scripts or not any(item["code"].strip() for item in parser.scripts):
        raise ValueError(f"{name}: expected executable scripts, found none")
    return {"name": name, "scripts": parser.scripts, "handlers": parser.handlers}


def render_pages() -> dict:


    from waam_pipeline import accounts, bead_test, oidc, passkeys, run_logs, run_sheet, server
    from waam_pipeline import studio, ui_preferences, viewer, web_assets
    from waam_pipeline.toolpath import Move, Toolpath

    source_dir = ROOT / "src" / "waam_pipeline"
    vendor = (source_dir / "_vendor" / "three-r180.cjs").read_text(encoding="utf-8")
    controls = (source_dir / "studio_controls.js").read_text(encoding="utf-8")
    path = Toolpath([
        Move(0, 0, 0, 300, False, 0, 1),
        Move(20, 0, 0, 300, True, 0, 2),
    ])
    state = studio.build_state(path, title="Lint fixture", workbench=True,
                               workspace="lint-fixture", email="lint@example.invalid")
    cfg = oidc.AuthConfig(provider="local", client_id="", client_secret="",
                          public_url="https://lint.example.invalid")
    pages = []
    with tempfile.TemporaryDirectory(prefix="waam-lint-pages-") as directory:
        tmp = Path(directory)
        with mock.patch.multiple(
            server,
            ACCOUNTS=accounts.Store(tmp / "accounts.json"),
            WEBAUTHN=passkeys.WebAuthnStore(tmp / "passkeys.json"),
            UI_PREFERENCES=ui_preferences.Store(tmp / "preferences"),
            SAVES_DIR=tmp / "saves",
        ), mock.patch.object(
            server, "_refresh_auth", side_effect=AssertionError("Lint must not read live auth")
        ), mock.patch.object(passkeys, "available", return_value=True), mock.patch.object(
            web_assets, "assets", return_value={"renderer": {"key": "lint-renderer"}}
        ):
            for hosted in (False, True):
                for workbench in (False, True):
                    state["meta"]["workbench"] = workbench
                    markup = studio.render_studio(state, hosted=hosted)
                    if controls not in markup:
                        raise ValueError("Studio controls fragment is missing from rendered lint input")
                    if not hosted:
                        markup = remove_vendor(markup, vendor)
                    mode = "hosted" if hosted else "offline"
                    use = "workbench" if workbench else "preview"
                    pages.append(collect_page(f"studio-{mode}-{use}", markup))
            out = viewer.write_html(path, [], tmp / "viewer.html", "Lint fixture")
            pages.append(collect_page("viewer", out.read_text(encoding="utf-8")))
            pages.append(collect_page("signin-passkey", server._signin_page(cfg, mode="in")))
            pages.append(collect_page("admin", server._admin_page(cfg)))
            spec = bead_test.TestSpec.load(ROOT / "examples" / "bead_test_lc50s.json")
            pages.append(collect_page("bead-report", bead_test.render_report(spec)))
            row = {"layer": 0, "pass": None, "unit": "C", "pre_weld": 80,
                   "readings": {"20": 250, "40": 220, "60": 195}, "park_travel_s_est": 5,
                   "height_mm": 2.5, "height_source": "sense", "width_mm": 6, "note": ""}
            sheet = run_sheet.render_run_sheet(
                {"run": "2026-10-07_LINT_r01", "status": "open"},
                [row], ["20", "40", "60"], workspace="lint-fixture",
            )
            pages.append(collect_page("run-sheet", sheet))
            pages.append(collect_page("run-logs", run_logs.render_run_logs()))
    return {"pages": pages, "embedded_only": ["src/waam_pipeline/studio_controls.js"]}


if __name__ == "__main__":
    print(json.dumps(render_pages()))
