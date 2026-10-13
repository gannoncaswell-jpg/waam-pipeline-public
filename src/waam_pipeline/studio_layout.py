from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def controls_script() -> str:
    return Path(__file__).with_name("studio_controls.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def placement_script() -> str:
    return Path(__file__).with_name("studio_placement.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def help_script() -> str:
    return Path(__file__).with_name("studio_help.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def settings_script() -> str:
    return Path(__file__).with_name("studio_settings.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def window_script() -> str:
    return Path(__file__).with_name("studio_window.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def scale_script() -> str:
    return Path(__file__).with_name("studio_scale.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def settings_styles() -> str:
    return Path(__file__).with_name("studio_settings.css").read_text(encoding="utf-8")

STUDIO_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%% | WAAM Studio</title>
<style>
  :root {
    color-scheme: dark;
    --bg:#11171d; --panel:#192129; --panel2:#202a34; --line:#36434f;
    --ink:#eef3f8; --dim:#aebbc9; --accent:#31b1f4; --weld:#ffc86a;
    --ok:#77dda6; --bad:#ff8f83;
  }
  * {box-sizing:border-box; margin:0;}
  [hidden] {display:none!important;}
  html,body {height:100%;}
  body {background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI",system-ui,sans-serif;overflow:hidden;}
  button,input,select {font:inherit;}
  button,a,summary,input,select { -webkit-tap-highlight-color:transparent; }
  button,select,input[type="number"],input[type="text"],input[type="search"],.button {
    border:1px solid var(--line);border-radius:7px;background:var(--panel2);color:var(--ink);
    min-height:40px;padding:8px 11px;
  }
  button,.button,summary {cursor:pointer;}
  button:hover,.button:hover,summary:hover {background:#2a3946;border-color:#637b8d;}
  button:disabled {opacity:.42;cursor:not-allowed;}
  button:focus-visible,a:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible,canvas:focus-visible,#view:focus-visible {
    outline:2px solid var(--accent);outline-offset:3px;
  }
  a {color:var(--accent);text-decoration:none;}
  input[type="checkbox"] {accent-color:var(--accent);width:17px;height:17px;flex-shrink:0;}
  input[type="number"] {width:100%;font-variant-numeric:tabular-nums;}
  input[type="range"] {accent-color:var(--accent);cursor:pointer;min-width:50px;}
  .corner-entry {display:grid;grid-template-columns:minmax(0,1fr) 92px;gap:6px 10px;margin:12px 0;}
  .corner-entry label {font-size:12px;color:var(--dim);}
  .corner-entry input {width:100%;min-width:0;padding:7px;font-variant-numeric:tabular-nums;}
  .substrate-diagram {display:block;width:150px;height:85px;margin:10px auto;color:var(--accent);}
  .surface-readout {border:1px solid var(--line);border-radius:8px;padding:12px;margin:12px 0;background:#18232d;}
  .surface-readout .surface-state {margin:0 0 10px;font-size:13px;font-weight:600;color:var(--dim);}
  .surface-readout[data-state="ready"] .surface-state {color:#80d9b0;}
  .surface-readout[data-state="pending"] .surface-state {color:#f3c879;}
  .surface-height {display:flex;flex-direction:column;gap:4px;margin-bottom:12px;}
  .surface-height span {font-size:12px;color:var(--dim);}
  .surface-height output {font-size:24px;font-weight:600;font-variant-numeric:tabular-nums;}
  .surface-readout .row {font-size:12px;gap:10px;}
  .surface-readout output {text-align:right;font-variant-numeric:tabular-nums;}
  #substrateToolSetupStatus {margin:10px 0;}
  .sr-only {position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0;}
  #app {height:100dvh;display:grid;grid-template:"head head" 64px "steps steps" 64px "stage side" minmax(0,1fr) / minmax(0,1fr) 350px;}
  header {grid-area:head;display:flex;align-items:center;gap:16px;padding:0 24px;border-bottom:1px solid var(--line);background:linear-gradient(120deg,#202932,#151d24);min-width:0;}
  .logo {font-size:22px;font-weight:650;letter-spacing:-.6px;white-space:nowrap;display:flex;align-items:center;gap:12px;}
  .brand-mark {color:var(--accent);font-size:29px;font-weight:900;font-style:italic;line-height:1;}
  header h1 {font-size:15px;font-weight:400;color:#cbd7e3;border-left:1px solid #52616e;padding-left:20px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .identity {margin-left:auto;display:flex;align-items:center;gap:10px;min-width:0;}
  .chip {font-size:12px;white-space:nowrap;}
  #wsChip {max-width:230px;overflow:hidden;text-overflow:ellipsis;}
  #signIn,#adminLink,#signOut {border:1px solid var(--line);padding:6px 10px;border-radius:6px;}
  #helpBtn {min-height:34px;padding:6px 12px;flex-shrink:0;}
  #helpDlg {margin:auto;width:min(660px,calc(100vw - 28px));max-height:calc(100dvh - 28px);padding:0;border:1px solid #63798a;border-radius:12px;background:var(--panel);color:var(--ink);box-shadow:0 24px 90px #0008;}
  #helpDlg[open] {display:flex;flex-direction:column;}
  #helpDlg::backdrop {background:#071018b8;}
  .help-heading {display:flex;align-items:center;justify-content:space-between;gap:16px;padding:20px 24px 14px;}
  .help-heading h2 {font-size:22px;font-weight:650;}
  #helpClose {min-height:34px;padding:4px 10px;}
  .help-tabs {display:flex;gap:6px;padding:0 24px 16px;border-bottom:1px solid var(--line);}
  .help-tabs button {flex:1;background:transparent;min-width:0;padding:7px 10px;font-size:13px;}
  .help-tabs [aria-selected="true"] {background:#103b54;border-color:var(--accent);color:#8ed9ff;}
  .help-body {padding:22px 24px;overflow-y:auto;min-height:0;line-height:1.6;}
  .help-body h3 {font-size:19px;margin:0 0 12px;}
  .help-body h4 {font-size:14px;margin:20px 0 10px;}
  .help-progress {font-size:12px;color:var(--accent);margin-bottom:8px;font-variant-numeric:tabular-nums;}
  #helpSteps {padding-left:20px;}
  #helpSteps li {margin:10px 0;padding-left:4px;}
  .help-context {margin-top:18px;padding:12px;border-left:3px solid var(--accent);background:#143044;color:#d0e5f4;font-size:13px;}
  .help-reference {display:grid;grid-template-columns:minmax(100px,.8fr) 1.8fr;gap:10px 18px;font-size:13px;}
  .help-reference dt {font-weight:600;color:var(--ink);}
  .help-reference dd {margin:0;color:var(--dim);}
  #helpDlg kbd {display:inline-block;border:1px solid #536879;border-radius:4px;padding:0 5px;background:#233341;color:var(--ink);font-family:inherit;font-size:12px;line-height:1.7;white-space:nowrap;}
  #helpDlg details {border-bottom:1px solid var(--line);padding:12px 0;}
  #helpDlg details:first-child {padding-top:0;}
  #helpDlg summary {font-weight:600;}
  #helpDlg details p {margin-top:10px;color:var(--dim);font-size:13px;}
  .help-actions {display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 24px;border-top:1px solid var(--line);flex-shrink:0;}
  .help-paging {display:flex;gap:8px;}
  .help-actions .primary {min-height:40px;min-width:80px;}
  #helpGo {font-size:13px;}
  @media (max-width:480px) {
    .help-heading,.help-body {padding:16px;}
    .help-tabs {padding:0 16px 12px;gap:4px;}
    .help-tabs button {padding:7px 5px;font-size:12px;}
    .help-actions {padding:12px 16px;flex-wrap:wrap;}
    .help-reference {grid-template-columns:1fr 1.5fr;gap:10px;}
  }
  .workflow {grid-area:steps;display:flex;align-items:stretch;border-bottom:1px solid var(--line);background:#1b242d;padding:6px 12px;gap:4px;}
  .workflow [role="tab"] {flex:1;display:flex;justify-content:center;align-items:center;gap:18px;border-color:transparent;background:transparent;border-radius:6px;color:var(--dim);font-size:15px;}
  .workflow [role="tab"] b {font-size:22px;font-weight:500;}
  .workflow [aria-selected="true"] {background:#086aa6;color:white;border-color:#1689c7;box-shadow:inset 0 1px #229bd2;}
  .workflow [aria-selected="false"]:hover {background:#283642;}
  .cell-name {width:326px;flex-shrink:0;align-self:center;text-align:right;padding-right:16px;color:#c6d2df;font-size:14px;}
  .stage {grid-area:stage;min-width:0;min-height:0;margin:12px 0 14px 14px;border:1px solid #465563;border-radius:9px;overflow:hidden;display:grid;grid-template-rows:50px minmax(0,1fr) 78px;background:#1c252e;}
  .view-toolbar {display:flex;align-items:center;padding:6px 12px;gap:8px;border-bottom:1px solid #536371;z-index:4;background:#1c252e;}
  .view-toolbar button,.view-toolbar select,.view-toolbar summary {min-height:34px;font-size:13px;padding:5px 12px;}
  .view-tabs {display:flex;margin-right:auto;}
  .view-tabs button {border-radius:0;background:transparent;min-width:91px;}
  .view-tabs button:first-child {border-radius:6px 0 0 6px;}
  .view-tabs button:last-child {border-radius:0 6px 6px 0;}
  .view-tabs [aria-pressed="true"] {color:#60cdff;border-color:#48baff;background:#113247;}
  .layers-menu {position:relative;}
  .layers-menu summary {list-style:none;border:1px solid var(--line);border-radius:6px;}
  .layers-menu summary::-webkit-details-marker {display:none;}
  .layers-body {position:absolute;top:42px;right:0;width:255px;padding:16px;border:1px solid #536779;background:#202a34;border-radius:8px;box-shadow:0 12px 35px #0006;}
  .layers-body h3 {margin:0 0 10px;font-size:14px;}
  .t {display:flex;align-items:center;gap:10px;min-height:35px;}
  #view {position:relative;min-height:0;overflow:hidden;background:#c8cdd2;}
  #view canvas {position:absolute;inset:0;width:100%;height:100%;display:block;}
  #c {touch-action:none;z-index:1;cursor:grab;}
  #c:active {cursor:grabbing;}
  .placement-tools {position:absolute;top:58px;left:14px;z-index:3;display:flex;gap:4px;padding:5px;background:#192b39f5;border:1px solid #607b90;border-radius:8px;box-shadow:0 3px 12px #0003;}
  .placement-tools button {min-height:35px;padding:5px 10px;font-size:12px;}
  .placement-tools [aria-pressed="true"] {background:#086aa6;border-color:#4bb8f6;color:white;}
  .placement-tools .tool-divider {border-left:1px solid #567084;margin:2px 3px;}
  .view-presets {position:absolute;right:12px;top:14px;z-index:3;display:flex;gap:3px;}
  .view-presets button {min-height:31px;padding:4px 8px;background:#edf4f8e8;color:#173349;border-color:#8fa4b4;font-size:11px;}
  #placementPreview {position:absolute;left:14px;top:110px;z-index:2;padding:6px 10px;border:1px solid #c18e31;border-radius:6px;background:#fff2cff2;color:#513805;font-size:12px;pointer-events:none;}
  .axis-x {color:#ff999c!important;} .axis-y {color:#79daa2!important;} .axis-z {color:#8cbeff!important;}
  .placement-actions {display:flex;gap:7px;margin-top:12px;}.placement-actions button{flex:1;}
  #clearanceBadge {position:absolute;top:16px;left:16px;z-index:2;border:1px solid #af803d;background:#493514ef;color:#ffe2a2;border-radius:16px;min-height:32px;padding:5px 12px;font-size:12px;box-shadow:0 2px 6px #0002;}
  #clearanceBadge[data-state="clear"] {background:#163e30ed;border-color:#558d6a;color:#b6ffd2;}
  #clearanceBadge[data-state="blocked"] {background:#642c25ef;border-color:#c56d61;color:#ffe3df;}
  #offer {position:absolute;bottom:10px;left:12px;right:12px;z-index:3;align-items:center;gap:12px;background:#152735f2;color:#d7e9f7;border:1px solid #567388;border-radius:8px;padding:12px;box-shadow:0 4px 20px #0003;font-size:12px;}
  #offerText {flex:1;}
  #offerBtn {white-space:nowrap;}
  #offerX {padding:0;width:28px;min-height:28px;}
  #busy {position:absolute;inset:0;background:#14212bbd;backdrop-filter:blur(3px);z-index:5;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:16px;color:white;}
  .spinner {width:28px;height:28px;border:3px solid #ffffff40;border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;}
  @keyframes spin {to{transform:rotate(360deg);}}
  .busy-detail {font-size:12px;color:#cad9e6;}
  footer {padding:9px 16px 7px;min-width:0;border-top:1px solid #4c5a68;}
  .transport {display:flex;align-items:center;gap:16px;}
  #play {width:43px;min-height:34px;padding:4px;font-size:17px;}
  #cursor {flex:1;}
  #layerReadout {font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums;}
  #speed {min-height:32px;padding:3px 6px;background:transparent;border:none;width:62px;}
  .transport-hints {display:flex;gap:10px;justify-content:space-between;font-size:11px;color:var(--dim);margin-top:5px;}
  #readout {overflow:hidden;white-space:nowrap;text-overflow:ellipsis;text-align:right;max-width:58%;font-variant-numeric:tabular-nums;}
  aside {grid-area:side;min-width:0;min-height:0;overflow-y:auto;scrollbar-width:thin;scrollbar-color:#52606c transparent;}
  #wb {min-height:100%;padding:24px 22px 18px;}
  .step-panel {display:flex;flex-direction:column;min-height:calc(100dvh - 204px);}
  .panel-title {font-size:24px;letter-spacing:-.4px;font-weight:650;margin-bottom:4px;line-height:1.25;}
  .intro {color:#c3d0dd;margin-bottom:22px;font-size:13px;}
  h3 {font-size:15px;font-weight:600;margin:0 0 14px;}
  section {padding:20px 0;border-bottom:1px solid var(--line);}
  section:first-of-type {padding-top:0;}
  .row {display:flex;align-items:center;justify-content:space-between;gap:12px;margin:10px 0;min-height:34px;}
  .row>label,.row>span:first-child {color:#c8d5e0;font-size:13px;}
  .row>select {max-width:164px;min-width:112px;}
  .row>input {max-width:140px;}
  .number-unit {display:flex;align-items:center;width:146px;flex-shrink:0;border:1px solid var(--line);border-radius:7px;background:#1d2731;}
  .number-unit input {border:0;background:transparent;min-width:0;width:85px;padding-right:0;}
  .number-unit span {font-size:12px;color:var(--dim);padding-right:12px;white-space:nowrap;margin-left:auto;}
  .field-label,[for="wbJobName"],[for="wbJbiName"] {display:block;font-size:12px;color:var(--dim);margin:12px 0 6px;}
  .full {width:100%;}
  .demo-card {display:flex;align-items:center;gap:14px;border:1px solid #485866;border-radius:9px;background:linear-gradient(120deg,#242f3a,#1b242c);padding:15px 13px;margin-bottom:12px;}
  .demo-card svg {width:48px;height:48px;color:#b9d0e0;flex-shrink:0;}
  .demo-card>div {min-width:0;flex:1;}
  .demo-card label {color:var(--dim);font-size:11px;}
  .demo-card select {display:block;background:transparent;border:0;padding:0 18px 0 0;width:100%;min-height:28px;font-weight:600;}
  .upload {position:relative;display:flex;align-items:center;justify-content:center;gap:10px;border:1px solid #65caff;border-radius:7px;padding:10px;cursor:pointer;min-height:44px;}
  .upload:hover {background:#153044;}
  .upload:focus-within {outline:2px solid var(--accent);outline-offset:3px;}
  .upload input {position:absolute;inset:0;opacity:0;width:100%;height:100%;cursor:pointer;}
  #sourceInfo {color:var(--dim);font-size:12px;margin:8px 0;overflow-wrap:anywhere;}
  #libraryPanel {margin-bottom:12px;}
  #libraryPanel select,#librarySearch {width:100%;margin:5px 0;min-width:0;}
  #libraryTools {display:flex;gap:8px;align-items:center;}
  #libraryTools select {flex:1;}
  #libraryStatus,#stlInspection {font-size:12px;overflow-wrap:anywhere;white-space:pre-line;margin:8px 0;}
  #stlInspection[data-state="warning"],#libraryStatus[data-state="error"],#sliceStatus[data-state="error"] {color:var(--bad);}
  #sliceStatus {text-align:left;font-size:12px;white-space:pre-line;overflow-wrap:anywhere;}
  #sliceStatus:empty {display:none;}
  #stlChip {display:flex;align-items:center;gap:10px;margin:10px 0;padding:8px 10px;border:1px solid var(--line);border-radius:7px;}
  #stlName {flex:1;min-width:0;overflow-wrap:anywhere;font-size:12px;}
  #clearStl {flex-shrink:0;}
  .text-button {background:transparent;border:0;color:var(--accent);padding:4px 0;min-height:28px;font-size:12px;}
  .accordion {border-bottom:1px solid var(--line);padding:14px 0;}
  .accordion>summary {font-weight:550;list-style:none;display:flex;align-items:center;justify-content:space-between;padding:4px 0;}
  .accordion>summary:after {content:'›';color:var(--dim);font-size:23px;font-weight:400;line-height:20px;}
  .accordion[open]>summary:after {transform:rotate(90deg);}
  .accordion>summary:hover {background:transparent;color:white;}
  .primary {background:var(--accent);color:#082333;border-color:#3abbfa;font-weight:650;min-height:48px;}
  .primary:hover {background:#60c9ff;border-color:#89d7ff;}
  .panel-action {margin-top:auto;padding:18px 0 10px;position:sticky;bottom:0;z-index:2;background:var(--bg);box-shadow:0 -12px 20px var(--bg);}
  .panel-action p {text-align:center;font-size:11px;color:var(--dim);margin-top:9px;}
  .empty,.help {font-size:12px;color:var(--dim);line-height:1.6;}
  .button-row {display:flex;gap:8px;margin:10px 0;}
  .button-row>* {flex:1;min-width:0;}
  .nudge-grid {display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0;}
  .status-card {border:1px solid var(--line);background:#1e2a34;border-radius:8px;padding:14px;margin:8px 0 16px;}
  .status-card[data-state="blocked"] {border-color:#89564e;background:#342523;}
  .status-card[data-state="clear"] {border-color:#497a61;background:#20352b;}
  .status-card strong {display:block;margin-bottom:6px;}
  .status-card p {font-size:12px;color:#c7d4df;}
  .violation-group {border-bottom:1px solid var(--line);padding:10px 0;}
  .violation-group summary {font-size:13px;font-weight:550;}
  .violation-group p {font-size:12px;color:var(--dim);margin:8px 0;}
  .violation-group button {width:100%;text-align:left;font-size:12px;min-height:34px;margin:3px 0;}
  .v {font-variant-numeric:tabular-nums;color:var(--ink);}
  .big {font-size:18px;}
  #paramChart {height:74px;width:100%;display:block;margin-top:12px;}
  #wbMsg {margin-top:15px;font-size:12px;color:#c6d9e7;overflow-wrap:anywhere;}
  #wbMsg:empty {display:none;}
  .field-error {color:var(--bad);font-size:12px;white-space:pre-line;}
  .field-error:empty {display:none;}
  input[aria-invalid="true"] {border-color:var(--bad);}
  #wbMsg,#placementStatus,#arcWarn {white-space:pre-line;}
  #placementStatus,#correctionStatus {margin-top:12px;}
  #arcWarn {font-size:12px;color:var(--weld);margin-top:8px;}
  #thermalBody {font-size:12px;line-height:1.6;}
  #thermalBody>strong {display:block;color:var(--weld);}
  #thermalBody p {margin:8px 0;color:var(--dim);overflow-wrap:anywhere;}
  #thermalBody details {padding:9px 0;border-bottom:1px solid var(--line);}
  #thermalBody summary {font-size:12px;}
  #thermalBody button {font-size:12px;min-height:32px;}
  .clean {color:var(--ok);font-size:13px;}
  #platterDlg {margin:auto;background:var(--panel);color:var(--ink);border:1px solid #657685;border-radius:13px;padding:26px;width:min(460px,calc(100vw - 32px));box-shadow:0 30px 100px #0008;}
  #platterDlg::backdrop {background:#071018aa;backdrop-filter:blur(4px);}
  #platterForm {display:flex;flex-direction:column;gap:13px;}
  #platterForm h2 {font-size:21px;}
  #platterForm .attestation {display:flex;align-items:flex-start;gap:12px;line-height:1.6;}
  #platterForm .attestation input {margin-top:4px;}
  #platterForm select,#platterForm input[type="text"] {width:100%;max-width:none;}
  #platterForm .button-row {margin-bottom:0;}
  @media (min-width: 1700px) {#app {grid-template-columns:minmax(0,1fr) 390px;}#wb {padding:28px;} .cell-name{width:366px;} .panel-title{font-size:27px;} .row{margin:14px 0;}}
  @media (max-width: 1100px) {#app{grid-template-columns:minmax(0,1fr) 320px;} #wb{padding:20px 16px;}header{padding:0 16px;gap:12px;} .cell-name{width:292px;}#wsChip{max-width:150px;} .view-toolbar{padding:6px 8px;gap:6px;}.view-tabs button{min-width:74px;padding:5px 8px;} .transport{gap:10px;} .number-unit{width:135px;}}
  @media (max-width: 860px) {
    html,body {height:auto;min-height:100%;} body{overflow-y:auto;}
    #app {height:auto;min-height:100dvh;grid-template:"head" auto "steps" 60px "stage" clamp(380px,54vh,620px) "side" auto / minmax(0,1fr);}
    header {min-height:62px;flex-wrap:wrap;padding:12px 16px;gap:8px 14px;}
    .logo{font-size:19px;}header h1{font-size:13px;padding-left:12px;flex:1;}
    .identity{font-size:11px;}#wsChip{max-width:180px;}
    .workflow{position:sticky;top:0;z-index:8;padding:6px 10px;}.workflow [role="tab"]{gap:8px;font-size:13px;}.workflow [role="tab"] b{font-size:18px;}
    .cell-name{display:none;}.stage{margin:10px 12px 0;grid-template-rows:48px minmax(0,1fr) 76px;}
    aside{overflow:visible;}#wb{padding:24px;max-width:640px;margin:0 auto;}.step-panel{min-height:0;}.panel-action{margin-top:20px;}
    #offer {font-size:11px;flex-wrap:wrap;padding:9px 35px 9px 10px;}#offerText{flex-basis:100%;}#offerX{position:absolute;top:8px;right:8px;}
    .number-unit{width:170px;}.number-unit input{width:105px;}.row{min-height:40px;}
  }
  @media (max-width: 480px) {
    .view-presets{top:auto;bottom:10px;right:10px;}
    .placement-tools{left:10px;top:56px;gap:2px;padding:4px;}.placement-tools button{padding:5px 8px;}
    #placementPreview{left:10px;right:10px;top:107px;}
    header .identity{width:100%;justify-content:flex-end;}header h1{max-width:42vw;}#wsChip{margin-right:auto;max-width:190px;}
    .stage{margin:8px 7px 0;}.view-toolbar{gap:5px;padding:6px;}.view-toolbar button,.view-toolbar summary,.view-toolbar select{font-size:11px;padding:5px 7px;}.view-tabs button{min-width:62px;}
    #layerReadout{font-size:11px;}.transport{gap:7px;}footer{padding:9px 10px;}#readout{display:none;}.transport-hints{font-size:10px;}#wb{padding:22px 18px;}.workflow [role="tab"]{gap:6px;font-size:12px;}
  }
  @media (prefers-reduced-motion:reduce) {.spinner{animation-duration:2s;}}
%%SETTINGS_CSS%%
</style>
</head>
<body data-look="graphite" data-density="comfortable">
<div id="app">
<header>
  <div class="logo" aria-label="WAAM STUDIO"><span class="brand-mark" aria-hidden="true">W</span>WAAM Studio</div>
  <h1 class="title">%%TITLE%%</h1>
  <div class="identity"><button class="chip" id="helpBtn" type="button" aria-haspopup="dialog" aria-controls="helpDlg">Help</button><button class="chip" id="settingsBtn" type="button" aria-haspopup="dialog" aria-controls="settingsDlg" aria-expanded="false">Settings</button><details class="layout-menu" id="layoutMenu"><summary class="chip">Layout</summary><div class="layout-body" role="group" aria-labelledby="layoutTitle">
      <h3 id="layoutTitle">Window layout</h3>
      <label class="layout-field" for="layoutWidth"><span>Build panel width</span><output id="layoutWidthValue" for="layoutWidth">Default</output></label>
      <input type="range" id="layoutWidth" min="280" max="760" step="20" value="350">
      <label class="layout-field" for="layoutScale"><span>Panel text size</span></label>
      <select id="layoutScale"><option value="0.9">Small (90%)</option><option value="1" selected>Normal</option><option value="1.15">Large (115%)</option><option value="1.3">Extra large (130%)</option></select>
      <div class="layout-actions"><button type="button" data-inspector-toggle="text" aria-pressed="false">Hide build panel</button><button type="button" id="layoutReset">Reset layout</button></div>
      <p class="layout-note">Drag the panel's left edge to resize it; double-click the edge to reset. <kbd>&#92;</kbd> hides or shows the panel. Saved in this browser.</p>
    </div></details><span class="chip" id="wsChip" style="display:none"></span>
    <a class="chip" id="signIn" href="/signin" style="display:none">Sign in</a>
    <a class="chip" id="adminLink" href="/admin" style="display:none">Admin</a>
    <a class="chip" id="signOut" href="/auth/logout" style="display:none">Sign out</a>
  </div>
</header>
<div id="passkeyOffer" role="region" aria-label="Set up a passkey" style="display:none;position:fixed;left:16px;right:16px;bottom:16px;z-index:60;max-width:540px;margin:0 auto;background:#0f2c42;border:1px solid #2f6d94;border-radius:10px;padding:12px 14px;box-shadow:0 8px 28px rgba(0,0,0,.45)">
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <span aria-hidden="true" style="font-size:20px">&#128273;</span>
    <span style="flex:1;min-width:200px;color:#e8f1f8">Add a passkey to sign in with Touch ID, Windows Hello, or your phone.</span>
    <button type="button" id="passkeyOfferAdd" class="primary">Add a passkey</button>
    <button type="button" id="passkeyOfferNo" class="chip">Not now</button>
  </div>
</div>
<nav class="workflow" role="tablist" aria-label="Build workflow">
  <button id="tab-prepare" role="tab" aria-controls="step-prepare" aria-selected="true" data-step="prepare"><b aria-hidden="true">1</b>Prepare</button>
  <button id="tab-place" role="tab" aria-controls="step-place" aria-selected="false" tabindex="-1" data-step="place"><b aria-hidden="true">2</b>Place</button>
  <button id="tab-review" role="tab" aria-controls="step-review" aria-selected="false" tabindex="-1" data-step="review"><b aria-hidden="true">3</b>Review</button>
  <button id="tab-export" role="tab" aria-controls="step-export" aria-selected="false" tabindex="-1" data-step="export"><b aria-hidden="true">4</b>Export</button>
  <span class="cell-name" id="cellChip"></span>
</nav>
<main class="stage" aria-label="Build preview">
  <div class="view-toolbar">
    <div class="view-tabs" role="group" aria-label="Camera framing"><button id="cellView" aria-pressed="true">Cell view</button><button id="partView" aria-pressed="false">Part view</button></div>
    <select id="renderStyle" aria-label="Render style"><option value="solid" selected>Solid</option><option value="wire">Wireframe</option></select>
    <details class="layers-menu" id="layersMenu"><summary aria-label="Display layers">&#x25C7; Layers</summary><div class="layers-body">
      <h3>Display layers</h3>
      %%DETAIL%%
      <label class="t" id="partToggleRow" style="display:none"><input type="checkbox" id="showPart" checked>Part model</label>
      <label class="t"><input type="checkbox" id="showTravel" checked>Travel moves</label>
      <label class="t"><input type="checkbox" id="showGhost" checked>Remaining path</label>
      <label class="t"><input type="checkbox" id="showTorch">Torch envelope</label>
      <label class="t"><input type="checkbox" id="showScene" checked>Work surface &amp; fixtures</label>
      <label class="t"><input type="checkbox" id="showScale" checked>Scale bars (Place)</label>
      <label class="t"><input type="checkbox" id="showFrame" checked>Cell frame</label>
      <label class="t"><input type="checkbox" id="showWalls">Cell walls</label>
      <label class="t"><input type="checkbox" id="showFumeHood" checked>Fume hood</label>
      <label class="t"><input type="checkbox" id="showBoxes">Collision volumes</label>
      <label class="t" id="robotToggleRow" style="display:none"><input type="checkbox" id="showRobot" checked>Robot</label>
      <label class="t" id="conduitToggleRow" style="display:none"><input type="checkbox" id="showConduit" checked>Wire conduit</label>
      <div class="empty" id="toolDisplayNote" style="display:none"></div>
    </div></details>
    <button id="fitView" title="Fit the current view">&#x26F6; Fit view</button>
    <button type="button" id="inspectorToggle" data-inspector-toggle aria-pressed="false" aria-label="Hide build panel" title="Hide build panel (&#92;)">&#x25E8;</button>
  </div>
  <div id="view" role="region" tabindex="0" aria-label="Part preview" aria-describedby="hud"><canvas id="gl" aria-hidden="true"></canvas><canvas id="c" tabindex="0" aria-label="3D cell preview. Drag to orbit. Shift drag or middle drag to pan. Scroll to zoom in Ortho or travel in Persp; Shift scroll for fine travel. F switches between part and cell views."></canvas>
    <button id="clearanceBadge" data-state="pending" title="Open clearance review">Clearance not checked</button>
    <div class="view-presets" role="group" aria-label="Standard views"><button data-camera="iso" title="Isometric view">Iso</button><button data-camera="top" title="Top view">Top</button><button data-camera="front" title="Front view">Front</button><button data-camera="right" title="Right view">Right</button><button id="projectionToggle" aria-label="Perspective projection" aria-pressed="false" title="Switch to perspective projection for free travel">Ortho</button></div>
    <div id="placementToolbar" class="placement-tools" role="toolbar" aria-label="Placement tools" hidden>
      <button data-place-mode="move" aria-pressed="true" title="Move along X or Y; drag the center to move on the work surface">Move</button>
      <button data-place-mode="rotate" aria-pressed="false" title="Rotate around the part's vertical axis">Rotate</button>
      <button data-place-mode="orbit" aria-pressed="false" title="Orbit the camera without moving the part">Orbit</button>
      <span class="tool-divider" aria-hidden="true"></span><button id="placeUndo" title="Undo placement" aria-label="Undo placement" disabled>↶</button><button id="placeRedo" title="Redo placement" aria-label="Redo placement" disabled>↷</button>
    </div>
    <div id="placementPreview" role="status" aria-live="polite" hidden></div>
    <div id="offer" style="display:none"><span id="offerText"></span><a id="offerBtn" href="/signin">Sign in or create an account</a><button id="offerX" title="Hide this" aria-label="Dismiss account message">&times;</button></div>
    <div id="busy" hidden role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span><strong id="busyText">Checking build…</strong><span class="busy-detail"></span></div>
  </div>
  <footer><div class="transport">
    <button id="play" title="Play / pause (Space)" aria-label="Play toolpath">&#9654;</button>
    <input type="range" id="cursor" min="0" value="0" step="1" aria-label="Toolpath move">
    <span id="layerReadout">Layer 0 / 0</span>
    <select id="speed" aria-label="Playback speed"><option value="60" selected>1&times;</option><option value="180">3&times;</option><option value="600">10&times;</option><option value="2400">40&times;</option></select>
  </div><div class="transport-hints"><span id="hud">Drag to orbit · Scroll to zoom · Shift / middle-drag to pan</span><span id="readout"></span></div></footer>
</main>
<div id="sideSplitter" role="separator" tabindex="0" aria-orientation="vertical" aria-controls="buildInspector" aria-label="Resize build panel" aria-valuemin="280" aria-valuemax="760" aria-valuenow="350" title="Drag to resize. Double-click to reset."></div>
<aside id="buildInspector" aria-label="Build inspector"><div id="wb">
  <div class="step-panel" id="step-prepare" role="tabpanel" aria-labelledby="tab-prepare">
    <h2 class="panel-title">Prepare build</h2><p class="intro">Choose a part and configure slicing.</p>
    <section><h3>Cell</h3>
      <div class="row"><label for="wbCellProfile">Cell profile</label><select id="wbCellProfile"><option value="lc50s">LC50S · AR1440</option><option value="hc10">AW-HC · HC10DTP preview</option></select></div>
      <div class="row" id="hcCleanerRow" hidden><label for="wbHcCleaner">Cleaning station</label><select id="wbHcCleaner"><option value="none">Unspecified</option><option value="yaskawa" selected>Yaskawa (installed)</option><option value="tregaskiss">Tregaskiss (reference)</option><option value="binzel">Binzel (reference)</option></select></div>
      <p class="help" id="cellProfileNote" role="status"></p>
    </section>
    <section>
      <div class="demo-card" id="demoPicker"><svg viewBox="0 0 60 60" fill="none" stroke="currentColor" aria-hidden="true"><path d="M8 22 43 8 53 14 53 41 19 55 8 49Z M8 22 19 28 53 14 M19 28V55 M43 8V35"/></svg><div><label for="wbShape">Demo part</label><select id="wbShape">%%DEMOS%%</select></div></div>
      <div id="libraryPanel" hidden>
        <div id="libraryTools"><label for="librarySelect">Part library</label><button id="libraryRefresh" type="button" aria-label="Refresh part library">Refresh</button></div>
        <select id="libraryScope" aria-label="Library scope" hidden><option value="mine">My library</option><option value="all">All users · admin</option></select>
        <input id="librarySearch" type="search" placeholder="Find a part" aria-label="Search part library">
        <select id="librarySelect" aria-label="Library part"><option value="">Loading library…</option></select>
        <p class="help" id="libraryDownloadRow" hidden><a id="libraryDownload" href="#" download>Download original STL</a></p>
        <p class="help">Uploads are saved automatically.</p>
        <div id="libraryStatus" role="status" aria-live="polite"></div>
      </div>
      <label class="upload" for="stlFile"><span aria-hidden="true">&#8613;</span>Upload STL<input type="file" id="stlFile" accept=".stl" aria-label="Upload STL part"></label>
      <div id="sourceInfo" role="status" aria-live="polite">Demo part selected</div>
      <div id="stlChip" style="display:none"><span id="stlName"></span><button type="button" class="text-button" id="clearStl" aria-label="Remove STL" title="Use the demo part">Remove STL</button></div>
      <div class="row" id="stlUnitsRow" hidden><label for="wbUnits">STL units</label><select id="wbUnits"><option value="auto" selected>Auto detect</option><option value="mm">Millimetres</option><option value="in">Inches</option><option value="m">Metres</option></select></div>
      <div id="stlInspection" role="status" aria-live="polite" hidden></div>
    </section>
    <section><h3>Slice settings</h3>
      <div class="row" id="beadLengthRow" hidden><label for="wbBeadLength">Bead length</label><div class="number-unit"><input id="wbBeadLength" type="number" value="101.6" %%wbBeadLength%%><span>mm</span></div></div>
      <div class="row" id="beadCountRow" hidden><label for="wbBeadCount">Bead count</label><input id="wbBeadCount" type="number" value="1" %%wbBeadCount%%></div>
      <div class="row" id="beadPitchRow" hidden><label for="wbBeadPitch">Bead spacing</label><div class="number-unit"><input id="wbBeadPitch" type="number" value="25" %%wbBeadPitch%%><span>mm</span></div></div>
      <div class="row" id="wallLengthRow" hidden><label for="wbWallLength">Wall length</label><div class="number-unit"><input id="wbWallLength" type="number" value="150" %%wbWallLength%%><span>mm</span></div></div>
      <div class="row" id="wallLayersRow" hidden><label for="wbWallLayers">Wall layers</label><input id="wbWallLayers" type="number" value="6" %%wbWallLayers%%></div>
      <div class="row" id="wallThicknessRow" hidden><label for="wbWallThickness">Wall thickness</label><div class="number-unit"><input id="wbWallThickness" type="number" value="0" aria-label="Wall thickness in millimetres, 0 for one bead" %%wbWallThickness%%><span>mm</span></div></div>
      <div class="row" id="copiesRow" hidden><label for="wbCopies">Copies</label><input id="wbCopies" type="number" value="1" %%wbCopies%%></div>
      <div class="row" id="copySpacingRow" hidden><label for="wbCopySpacing">Copy spacing</label><div class="number-unit"><input id="wbCopySpacing" type="number" value="38.6" %%wbCopySpacing%%><span>mm</span></div></div>
      <div class="row"><label for="wbLh">Bead height</label><div class="number-unit"><input id="wbLh" type="number" value="2.9" aria-label="Bead height in millimetres: the height each layer adds" %%wbLh%%><span>mm</span></div></div>
      <div class="row"><label for="wbBead">Bead width</label><div class="number-unit"><input id="wbBead" type="number" value="8.6" %%wbBead%%><span>mm</span></div></div>
      <div class="row"><label for="wbTravel">Travel speed</label><div class="number-unit"><input id="wbTravel" type="number" value="175" %%wbTravel%%><span>mm/min</span></div></div>
      <div class="row"><label for="wbWfs">Wire feed (optional)</label><div class="number-unit"><input id="wbWfs" type="number" aria-label="Wire feed in inches per minute; blank means not entered" aria-describedby="wfsHelp" %%wbWfs%%><span>in/min</span></div></div>
      <p class="help" id="wfsHelp">Predicts layer pitch only; never changes the job.</p>
      <div class="row"><label for="wbCtwd">Weld CTWD</label><div class="number-unit"><input id="wbCtwd" type="number" aria-label="Weld CTWD in millimetres; blank is the calibrated default" aria-describedby="ctwdHelp" %%wbCtwd%%><span>mm</span></div></div>
      <p class="help" id="ctwdHelp">Contact tip to work while welding. Default is the calibrated 3/8 in; Pulse and Rapid X want about 3/4 in. The welds are raised, and every move after them rises too so the longer wire clears the part; the approach and touches keep the default. <span id="ctwdStatus" role="status"></span></p>
      <p class="help" id="beadSettingsHelp">Bead height: height each layer adds — about 2/3 of a single bead's measured height. Use a bead preset or measure the wall's layer pitch. Each layer is laid one bead height above the last, and clearance models every bead at that size. Bead width is the measured width. Travel speed is the weld speed of every pass. Beads in a set run parallel in +X. The solver prefers center spacing at 63–77% of bead width; when no bead count fits that range, it chooses the nearest overlapping count (or a gapped count only if none overlaps) and warns.</p>
    </section>
    <section><h3>Slicing</h3>
      <div class="row"><label for="wbSlicer">Slicing engine</label><select id="wbSlicer"><option value="builtin">Built-in · thin walls</option><option value="ornl">ORNL · solid parts</option></select></div>
      <p class="help" id="ornlEngineStatus" role="status"></p>
      <div id="ornlOptions" hidden>
        <div class="row"><label for="wbContours">Contour passes</label><input id="wbContours" type="number" value="2" %%wbContours%%></div>
        <div class="row"><label for="wbInfill">Infill pattern</label><select id="wbInfill"><option value="lines">Alternating lines</option><option value="concentric">Concentric</option><option value="none">Contours only</option></select></div>
        <div class="row"><label for="wbStepover">Bead spacing</label><div class="number-unit"><input id="wbStepover" type="number" value="75" %%wbStepover%%><span>% width</span></div></div>
        <div class="row"><label for="wbInfillAngle">Infill angle</label><div class="number-unit"><input id="wbInfillAngle" type="number" value="0" %%wbInfillAngle%%><span>deg</span></div></div>
        <div class="row"><label for="wbInfillRotation">Rotate each layer</label><div class="number-unit"><input id="wbInfillRotation" type="number" value="90" %%wbInfillRotation%%><span>deg</span></div></div>
        <div class="row"><label for="wbTravelLift">Travel lift</label><div class="number-unit"><input id="wbTravelLift" type="number" value="5" %%wbTravelLift%%><span>mm</span></div></div>
        <p class="help">Set spacing from measured bead width. Confirm fusion with a weld trial.</p>
      </div>
    </section>
    <details class="accordion"><summary>Material &amp; wire</summary>
      <div class="row"><label for="wbFiller">Filler metal</label><select id="wbFiller">%%FILLERS%%</select></div>
      <div class="row"><label for="wbWire">Wire diameter</label><div class="number-unit"><input id="wbWire" type="number" value="1.143" %%wbWire%%><span>mm</span></div></div>
    </details>
    <details class="accordion"><summary>Interpass simulation</summary>
      <div class="row"><label for="wbThermal">Simulation profile</label><select id="wbThermal"><option value="off">Off</option><option value="illustrative">Illustrative inputs</option><option value="profile">Uploaded profile</option></select></div>
      <div class="row"><label for="wbCooling">Cooling policy</label><select id="wbCooling"><option value="adaptive">Adaptive cooling</option><option value="continuous">Uninterrupted passes</option></select></div>
      <label class="upload" for="thermalFile">Upload simulation profile<input id="thermalFile" type="file" accept=".json,application/json"></label>
      <input id="wbThermalProfile" type="hidden" value="" aria-label="Loaded thermal profile data">
      <p class="help">Compare pass timing and cooling. Example profiles require cell validation.</p>
    </details>
    <details class="accordion"><summary>Advanced settings</summary>
      <div class="row"><label for="wbTilt">Part tilt</label><div class="number-unit"><input id="wbTilt" type="number" value="0" %%wbTilt%%><span>deg</span></div></div>
      <p class="help">Part tilt requests a Y rotation and snaps to a supporting face. Use Place for X and Y tilt.</p>
      <div class="row" id="seamRow"><label for="wbSeam">Seam per layer</label><div class="number-unit"><input id="wbSeam" type="number" value="25" %%wbSeam%%><span>deg</span></div></div>
      <div class="row"><label for="wbMargin">Clearance margin</label><div class="number-unit"><input id="wbMargin" type="number" value="5.0" %%wbMargin%%><span>mm</span></div></div>
      <p class="help" id="marginHelp">Minimum clearance: 5 mm.</p>
    </details>
    <div class="panel-action"><button id="sliceBtn" class="primary full">Slice &amp; check <span aria-hidden="true">&#8250;</span></button><p id="sliceStatus" role="status" aria-live="polite"></p><p id="sliceHelp">Next: confirm placement and platter setup.</p></div>
  </div>
  <div class="step-panel" id="step-place" role="tabpanel" aria-labelledby="tab-place" hidden>
    <h2 class="panel-title" id="placeTitle">Place on platter</h2><p class="intro">Position the part and match the physical setup.</p>
    <details class="accordion" id="substratePanel"><summary>Measured surface setup</summary>
      <p class="help" id="substrateIntro">Select the LC50S cell, then record four corners.</p>
      <h3>1. Record four corners</h3>
      <svg class="substrate-diagram" viewBox="0 0 150 85" role="img" aria-label="Corner order: 1 lower left, 2 lower right, 3 upper right, 4 upper left. X follows 1 to 2."><path d="M20 65V20H125V65Z" fill="none" stroke="currentColor"/><path d="M37 72H105l-7-4m7 4-7 4" fill="none" stroke="currentColor"/><g fill="currentColor" font-size="13"><text x="6" y="72">1</text><text x="130" y="72">2</text><text x="130" y="22">3</text><text x="6" y="22">4</text><text x="67" y="83">X</text></g></svg>
      <p class="help">Use TOOL 0 with the positioner at its registered home angle. Walk around the top perimeter in order. Enter each PULSE reading as S, L, U, R, B, T.</p>
      <p class="help" id="substrateGapHint">Measure each gap perpendicular to the substrate, from the metal contact tip to the top surface.</p>
      <div class="corner-entry"><label for="wbCorner1">Corner 1 · pulses</label><label for="wbTipGap1">Tip gap (mm)</label><input id="wbCorner1" type="text" autocomplete="off" placeholder="S, L, U, R, B, T"><input id="wbTipGap1" type="text" inputmode="decimal" autocomplete="off"></div>
      <div class="corner-entry"><label for="wbCorner2">Corner 2 · pulses</label><label for="wbTipGap2">Tip gap (mm)</label><input id="wbCorner2" type="text" autocomplete="off" placeholder="S, L, U, R, B, T"><input id="wbTipGap2" type="text" inputmode="decimal" autocomplete="off"></div>
      <div class="corner-entry"><label for="wbCorner3">Corner 3 · pulses</label><label for="wbTipGap3">Tip gap (mm)</label><input id="wbCorner3" type="text" autocomplete="off" placeholder="S, L, U, R, B, T"><input id="wbTipGap3" type="text" inputmode="decimal" autocomplete="off"></div>
      <div class="corner-entry"><label for="wbCorner4">Corner 4 · pulses</label><label for="wbTipGap4">Tip gap (mm)</label><input id="wbCorner4" type="text" autocomplete="off" placeholder="S, L, U, R, B, T"><input id="wbTipGap4" type="text" inputmode="decimal" autocomplete="off"></div>
      <label class="t" for="wbTouchCorners"><input type="checkbox" id="wbTouchCorners" aria-describedby="substrateTouchHint">Wire tip touching at each corner</label>
      <p class="help" id="substrateTouchHint">Tick when the CLEAN_R1-trimmed wire tip touched each corner instead of a ruler gap. Each gap becomes the TOOL 0 distance, measured along the wire.</p>
      <div class="row" id="substrateImportRow"><span>Fill pulses from VAR.DAT</span><button id="wbVarDatBtn" type="button">Choose file…</button><input id="wbVarDatFile" type="file" accept=".DAT,.dat,.txt" aria-label="VAR.DAT export file" hidden></div>
      <p class="help" id="substrateImportHint">Optional: import the four corner pulses (P040–P043) from an exported VAR.DAT. Then enter each ruler gap, or tick wire tip touching.</p>
      <p class="help" id="substrateToolSetupStatus">One-time tool setup is required before calculating this surface.</p>
      <details class="accordion" id="substrateToolSetup"><summary>Measurement &amp; tool setup</summary>
        <div class="row"><label for="wbSubstrateGapDirection">Ruler direction</label><select id="wbSubstrateGapDirection"><option value="normal" selected>Perpendicular to substrate</option><option value="vertical">Straight down vertically</option><option value="torch">Along the wire axis</option></select></div>
        <div class="row"><label for="wbTool0TipOffset">Contact tip to TOOL 0 endpoint</label><div class="number-unit"><input id="wbTool0TipOffset" type="text" inputmode="decimal" autocomplete="off" placeholder="Required measured distance" aria-describedby="substrateToolOffsetHelp substrateToolOffsetError"><span>mm</span></div></div>
        <p class="help" id="substrateToolOffsetHelp">Enter the verified distance along the wire axis from the metal contact tip to the calibrated TOOL 0 endpoint. Positive is toward the work. This fixed tool measurement differs from the ruler gaps and welding distance. If unknown, leave it blank and have the calibration owner measure it.</p>
        <p class="field-error" id="substrateToolOffsetError" role="alert" hidden></p>
        <div class="row"><span>Welding contact-tip distance</span><output id="registrationCtwd">Read from the cell profile</output></div>
        <p class="help">The calculation accounts for the recorded torch angles. The first weld runs with the contact tip at this profile distance above the measured top. Plate thickness only draws the plate below that top.</p>
      </details>
      <div class="row"><label for="wbSubstrateMargin">Edge margin</label><div class="number-unit"><input id="wbSubstrateMargin" type="number" value="5" %%wbSubstrateMargin%%><span>mm</span></div></div>
      <input id="wbSubstrateRegistration" type="hidden" value="" aria-label="Accepted substrate registration">
      <button id="registerSubstrate" type="button" class="primary full">Calculate &amp; apply surface</button>
      <p class="help" id="substrateStatus" role="status" aria-live="polite">Enter the four corners to calculate the surface.</p>
      <h3>2. Review height &amp; footprint</h3>
      <div class="surface-readout" id="substrateReadout" data-state="empty">
        <p class="surface-state" id="substrateReadoutState">Surface not calculated</p>
        <div class="surface-height"><span>Height above platter reference · center</span><output id="substrateHeight">—</output></div>
        <p class="help">Uses the current registered platter reference.</p>
        <div class="row"><span>Height across the surface</span><output id="substrateHeightRange">—</output></div>
        <div class="row"><span>X coverage · forward</span><output id="substrateCoverageX">—</output></div>
        <div class="row"><span>Y coverage · left</span><output id="substrateCoverageY">—</output></div>
        <p class="help">X and Y are measured from the platter center.</p>
        <div class="row"><span>Footprint size</span><output id="substrateFootprint">—</output></div>
        <p class="help">The bead stays inside the four measured edges, including the edge margin. X/Y ranges summarize the footprint.</p>
      </div>
      <details class="accordion"><summary>Sense the surface</summary>
        <p class="help">The four corners bound the clear area. Touches inside them measure the real surface instead of assuming a flat plate. The sense job needs the usual two-person dry-run review.</p>
        <div class="row"><label for="wbSenseLayout">Touch layout</label><select id="wbSenseLayout" disabled><option value="grid">Grid over the plate</option><option value="path">Along the part's base layer</option></select></div>
        <p class="help">Path stations sit where the part is welded. A small spacing, such as 10 mm, spreads the maximum number of touches along it.</p>
        <div class="row"><label for="wbSenseSpacing">Touch spacing</label><div class="number-unit"><input id="wbSenseSpacing" type="number" value="40" %%wbSenseSpacing%% disabled><span>mm</span></div></div>
        <div class="row"><label for="wbSenseInset">Inset from edges</label><div class="number-unit"><input id="wbSenseInset" type="number" value="10" %%wbSenseInset%% disabled><span>mm</span></div></div>
        <div class="row"><label for="wbSensePoints">Maximum touches</label><input id="wbSensePoints" type="number" value="21" %%wbSensePoints%% disabled></div>
        <div class="row"><label for="wbSenseName">Job name</label><input id="wbSenseName" type="text" value="SENSE_PLATE" autocomplete="off" disabled></div>
        <button id="wbSenseJobBtn" type="button" class="full" disabled>Download sense job</button>
        <div class="row"><label for="wbSenseFit">Surface fit</label><select id="wbSenseFit" disabled><option value="smooth">Smooth fit (refuses outliers)</option><option value="interpolate">Follow every touch</option></select></div>
        <p class="help">"Follow every touch" is exact at each touch and linear between them, and refuses a narrow low reading.</p>
        <div class="row"><span>VAR.DAT</span><button id="wbSenseVarDatBtn" type="button" disabled>Import sensed touches (P044…)</button><input id="wbSenseVarDatFile" type="file" accept=".DAT,.dat,.txt" aria-label="Sensed touches VAR.DAT export file" hidden disabled></div>
        <button id="wbSenseClearBtn" type="button" class="full" disabled>Remove sensed touches</button>
        <p class="help" id="senseStatus" role="status" aria-live="polite"></p>
        <p class="help" id="senseReadout">Not sensed</p>
      </details>
      <details class="accordion"><summary>Surface tilt &amp; fit</summary><p class="help" id="substrateFitDetails">Available after calculation.</p></details>
      <button id="clearSubstrate" type="button" class="full" disabled>Clear measured surface</button>
      <p class="help">Save the build to reuse this setup. Remeasure after moving or replacing the substrate.</p>
    </details>
    <section><h3>Part position</h3>
      <p class="help" id="positionHelp">Drag the handles or enter offsets from the default position.</p>
      <p class="place-tape" id="placeTape" hidden></p>
      <div class="row"><label class="axis-x" id="offsetXLabel" for="wbOx">X · forward</label><div class="number-unit"><input id="wbOx" type="number" value="0" %%wbOx%%><span>mm</span></div></div>
      <div class="row"><label class="axis-y" id="offsetYLabel" for="wbOy">Y · left</label><div class="number-unit"><input id="wbOy" type="number" value="0" %%wbOy%%><span>mm</span></div></div>
      <div class="row"><label class="axis-z" id="yawLabel" for="wbYaw">Z rotation</label><div class="number-unit"><input id="wbYaw" type="number" value="0" %%wbYaw%%><span>deg</span></div></div>
      <div class="row"><label for="placeStep">Move increment</label><select id="placeStep"><option value="0.1">0.1 mm</option><option value="1" selected>1 mm</option><option value="5">5 mm</option><option value="10">10 mm</option></select></div>
      <div class="nudge-grid"><button data-place-nudge="x,-1" aria-label="Move part backward">X −</button><button data-place-nudge="x,1" aria-label="Move part forward">X +</button><button data-place-nudge="y,-1" aria-label="Move part right">Y −</button><button data-place-nudge="y,1" aria-label="Move part left">Y +</button></div>
      <div class="row"><label for="rotateStep">Rotation increment</label><select id="rotateStep"><option value="1">1°</option><option value="5">5°</option><option value="15" selected>15°</option><option value="45">45°</option><option value="90">90°</option></select></div>
      <div class="nudge-grid"><button data-place-nudge="yaw,-1" aria-label="Rotate part clockwise">↶ Rotate −</button><button data-place-nudge="yaw,1" aria-label="Rotate part counterclockwise">↷ Rotate +</button></div>
      <div class="row"><label for="placeSnap">Drag grid</label><select id="placeSnap"><option value="0">Free</option><option value="1" selected>1 mm</option><option value="5">5 mm</option><option value="10">10 mm</option></select></div>
      <div class="row"><label for="rotateSnap">Rotation snap</label><select id="rotateSnap"><option value="0">Free</option><option value="1">1°</option><option value="5">5°</option><option value="15" selected>15°</option></select></div>
      <p class="help">Hold Alt for free movement. Release a handle to apply the new position.</p>
      <input id="movePart" type="checkbox" aria-label="Placement handles enabled" hidden>
      <div class="placement-actions"><button id="placeCenter">Center on platter</button><button id="placeRevert" disabled>Revert preview</button></div>
    </section>
    <section><h3>Place on face</h3>
      <div class="row"><label class="axis-x" for="wbRotX">X tilt</label><div class="number-unit"><input id="wbRotX" type="number" value="0" %%wbRotX%%><span>deg</span></div></div>
      <div class="row"><label class="axis-y" for="wbRotY">Y tilt</label><div class="number-unit"><input id="wbRotY" type="number" value="0" %%wbRotY%%><span>deg</span></div></div>
      <input id="wbLayFlat" type="hidden" value="0" min="0" max="1" aria-label="Lay flat enabled">
      <p class="help" id="faceStatus" role="status" aria-live="polite">Angles choose the nearest supporting face. Changing a tilt snaps and reslices.</p>
      <p class="help">Angles are relative to the imported part; Z rotation turns the seated part. Face placement clears the lift-off gap.</p>
      <div class="placement-actions"><button id="placeLayFlat">Lay flat</button><button id="placeResetOrientation">Reset orientation</button></div>
    </section>
    <section><h3 id="surfaceTitle">Platter setup</h3>
      <div class="row"><label for="wbSub" id="subLabel">Substrate thickness</label><div class="number-unit"><input id="wbSub" type="number" value="6.35" %%wbSub%%><span>mm</span></div></div>
      <div class="row"><label for="wbOz">Lift-off gap</label><div class="number-unit"><input id="wbOz" type="number" value="0" %%wbOz%%><span>mm</span></div></div>
      <div class="row" id="trainingBlockRow"><label for="wbFixture">Training block</label><select id="wbFixture"><option value="1">On platter</option><option value="0">Removed</option></select></div>
      <p class="help" id="surfaceHelp">Set substrate thickness and match the training block to the physical setup.</p>
      <button id="snapBtn" class="full" style="margin-top:12px">Snap to substrate</button>
      <div id="placementStatus" class="empty" role="status" aria-live="polite"></div>
    </section>
    <div class="panel-action"><button id="placeBtn" class="primary full">Apply placement &amp; check</button><p>Face changes reslice the part and update clearance.</p></div>
    <button class="full" data-next="review">Review checked build &#8250;</button>
  </div>
  <div class="step-panel" id="step-review" role="tabpanel" aria-labelledby="tab-review" hidden>
    <h2 class="panel-title">Review build</h2><p class="intro">Inspect clearance and replay the toolpath.</p>
    <div id="reviewVerdict" class="status-card"><strong>Clearance</strong><p>Checking status…</p></div>
    <section class="workbench-only"><h3>Build readiness</h3><ul id="buildReadiness" class="help" aria-live="polite"></ul></section>
    <button id="showCompleted" class="full">Show completed build</button>
    <section><h3>Slicing result</h3><p id="slicingSummary" class="help"></p></section>
    <section><h3>Interpass &amp; bead support</h3><div id="thermalBody" aria-live="polite"></div></section>
    <section><h3>Clearance findings</h3><div id="viol"></div></section>
    <section class="workbench-only"><h3>Travel correction</h3>
      <div class="row"><label for="wbRepair">Travel collisions</label><select id="wbRepair"><option value="1">Auto correct</option><option value="0">Check only</option></select></div>
      <p class="help">Auto correction adjusts travel moves. Bead collisions require changes to the part or slicing.</p>
      <button id="correctBtn" class="full" style="margin-top:12px">Find clear placement</button>
      <div id="correctionStatus" class="empty" role="status" aria-live="polite"></div>
    </section>
    <details class="accordion"><summary>Build &amp; weld parameters</summary>
      <div class="row"><span>Layer</span><span class="v big" id="layerNow">0</span></div><div class="row"><span>Deposited</span><span class="v" id="depNow">0 mm</span></div><div class="row"><span>Moves</span><span class="v" id="moveNow">0</span></div>
      <div id="paramRows"><div class="empty">No schedule attached</div></div><canvas id="paramChart" aria-label="Weld parameter chart"></canvas>
      <section id="arcPanel"><h3>Arc parameters</h3><div id="arcBody"></div><div id="arcWarn"></div></section>
    </details>
    <details class="accordion"><summary>Live feedback</summary><div id="liveRows"><div class="empty">No live welder connection.</div></div></details>
    <div class="panel-action workbench-only"><button class="primary full" data-next="export">Continue to export <span aria-hidden="true">&#8250;</span></button></div>
  </div>
  <div class="step-panel" id="step-export" role="tabpanel" aria-labelledby="tab-export" hidden>
    <h2 class="panel-title">Save &amp; export</h2><p class="intro">Save the setup or export a checked toolpath.</p>
    <div class="status-card" id="exportVerdict"><strong>Clearance</strong><p>Review the checked build before exporting.</p></div>
    <section><h3>Saved builds <span id="wbWho" class="sr-only"></span></h3>
      <label for="wbJobName">Save this setup as</label><div class="button-row"><input id="wbJobName" type="text" maxlength="40" placeholder="e.g. Wall trial"><button id="saveBtn" disabled>Save</button></div>
      <p class="help">Start with a letter or digit. Up to 40 letters, digits, spaces, underscores or hyphens.</p>
      <label for="wbJobs" class="field-label">Your builds</label><div class="button-row"><select id="wbJobs" aria-label="Saved builds"></select><button id="loadBtn" disabled>Load</button></div>
    </section>
    <section id="passkeySection" style="display:none"><h3>Passkeys</h3>
      <p class="help">Sign in with Touch ID, Windows Hello, or your phone.</p>
      <div id="passkeyList"></div>
      <button id="passkeyAdd" type="button" class="primary full" style="margin-top:8px">Add a passkey</button>
      <p id="passkeyMsg" class="help" role="status" aria-live="polite"></p>
    </section>
    <script data-passkeys>
    (function(){
      var sec=document.getElementById('passkeySection'),list=document.getElementById('passkeyList'),
          add=document.getElementById('passkeyAdd'),msg=document.getElementById('passkeyMsg'),
          offer=document.getElementById('passkeyOffer'),offerAdd=document.getElementById('passkeyOfferAdd'),
          offerNo=document.getElementById('passkeyOfferNo');
      if(!sec||!window.PublicKeyCredential){return;}
      var DISMISS='waamPasskeyOfferDismissed';
      function draftBlocked(){
        var reason=window.waamDraft&&window.waamDraft.blocked&&window.waamDraft.blocked();
        if(reason!==null&&reason!==undefined){msg.textContent=reason;return true;}
        return false;
      }
      function dismissed(){try{return localStorage.getItem(DISMISS)==='1';}catch(e){return false;}}
      function showOffer(on){if(offer){offer.style.display=(on&&!dismissed())?'block':'none';}}
      function u2b(s){s=s.replace(/-/g,'+').replace(/_/g,'/');s+='='.repeat((4-s.length%4)%4);var b=atob(s),a=new Uint8Array(b.length);for(var i=0;i<b.length;i++)a[i]=b.charCodeAt(i);return a.buffer;}
      function b2u(buf){var a=new Uint8Array(buf),s='';for(var i=0;i<a.length;i++)s+=String.fromCharCode(a[i]);return btoa(s).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');}
      function esc(t){var d=document.createElement('div');d.textContent=t||'';return d.innerHTML;}
      async function refresh(){
        var r; try{r=await fetch('/api/passkey/list',{credentials:'same-origin'});}catch(e){sec.style.display='none';showOffer(false);return;}
        if(r.status!==200){sec.style.display='none';showOffer(false);return;}
        var j=await r.json(); sec.style.display='';
        var has=j.passkeys&&j.passkeys.length;
        showOffer(!has);
        if(!has){list.innerHTML='<p class="help">No passkeys added yet.</p>';return;}
        list.innerHTML='';
        j.passkeys.forEach(function(p){
          var row=document.createElement('div');row.className='button-row';
          row.innerHTML='<span style="flex:1">'+esc(p.label)+'</span>';
          var rm=document.createElement('button');rm.type='button';rm.textContent='Remove';
          rm.onclick=async function(){if(draftBlocked())return;rm.disabled=true;try{await fetch('/api/passkey/remove',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:p.id})});}catch(e){} refresh();};
          row.appendChild(rm);list.appendChild(row);
        });
      }
      async function enroll(btn){
        if(draftBlocked())return false;
        msg.textContent='Follow your device prompt…';if(btn){btn.disabled=true;}
        try{
          var r=await fetch('/api/passkey/register/options',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:'{}'});
          var j=await r.json(); if(!j.ok){msg.textContent=j.error||'Could not start.';if(btn){btn.disabled=false;}return;}
          var o=j.options; o.challenge=u2b(o.challenge); o.user.id=u2b(o.user.id);
          if(o.excludeCredentials){o.excludeCredentials.forEach(function(c){c.id=u2b(c.id);});}
          var att; try{att=await navigator.credentials.create({publicKey:o});}catch(e){msg.textContent='Cancelled or unavailable.';if(btn){btn.disabled=false;}return;}
          var label=prompt('Name this passkey (e.g. "my laptop"):','my device')||'passkey';
          var cred={id:att.id,rawId:b2u(att.rawId),type:att.type,response:{attestationObject:b2u(att.response.attestationObject),clientDataJSON:b2u(att.response.clientDataJSON)},clientExtensionResults:{}};
          r=await fetch('/api/passkey/register/verify',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({handle:j.handle,credential:cred,label:label})});
          j=await r.json(); msg.textContent=j.ok?'Passkey added.':(j.error||'Could not add passkey.');
        }catch(e){msg.textContent='Could not add passkey.';}
        if(btn){btn.disabled=false;}refresh();
      }
      add.onclick=function(){enroll(add);};
      if(offerAdd){offerAdd.onclick=function(){enroll(offerAdd);};}
      if(offerNo){offerNo.onclick=function(){try{localStorage.setItem(DISMISS,'1');}catch(e){}showOffer(false);};}
      refresh();
    })();
    </script>
    <section><h3>Robot program</h3>
      <div class="row" role="radiogroup" aria-label="Build mode"><span>Build mode</span><label><input id="wbBuildManual" type="radio" name="wbBuildModeChoice" value="manual" checked disabled> Manual</label><label><input id="wbBuildAuto" type="radio" name="wbBuildModeChoice" value="auto" disabled> Auto</label></div>
      <input id="wbBuildMode" type="hidden" value="manual" aria-label="Build mode">
      <label for="wbJbiName">Controller job name (INFORM)</label><input id="wbJbiName" type="text" maxlength="32" pattern="[A-Z0-9][A-Z0-9_\\-]{0,31}" required aria-describedby="jobNameHint" placeholder="WALL_TRIAL" class="full">
      <p id="jobNameHint" class="help">1–32 characters: letters, digits, hyphen or underscore. Start with a letter or digit.</p>
      <label for="wbJobFolder" class="field-label">Controller job folder</label><input id="wbJobFolder" type="text" maxlength="32" pattern="[A-Za-z0-9_\\-]{1,32}" value="WAAM" autocomplete="off" spellcheck="false" aria-describedby="jobFolderHint" class="full">
      <p id="jobFolderHint" class="help">Robot and touch jobs load into this folder. Type it exactly as the pendant's JOB FOLDER LIST shows it. Blank or NONE means no folder.</p>
      <label for="wbArc" class="field-label">Arc schedule file (ASF#)</label><input id="wbArc" type="number" placeholder="From controller pendant" class="full" %%wbArc%%>
      <p class="help" style="margin-top:8px">Use the ASF number shown on the controller pendant.</p>
      <label for="wbArcSchedule" class="field-label">ASF# by layer (optional)</label><input id="wbArcSchedule" type="text" maxlength="400" placeholder="0:81;1-:83" autocomplete="off" spellcheck="false" aria-describedby="arcScheduleHint" class="full">
      <p id="arcScheduleHint" class="help">Replaces the single ASF#. Layers count from 0, as in Layers to print; 1- runs to the last layer; every layer exactly once. AEF# = ASF#.</p>
      <div class="row"><label for="wbInterpass">Interpass dwell</label><div class="number-unit"><input id="wbInterpass" type="number" value="30" %%wbInterpass%%><span>s</span></div></div>
      <div class="row"><label for="wbInterpassEvery">Layers per interpass</label><input id="wbInterpassEvery" type="number" value="1" %%wbInterpassEvery%%></div>
      <div class="row"><label for="wbTempCheckEvery">Check temperature every N layers</label><input id="wbTempCheckEvery" type="number" value="0" %%wbTempCheckEvery%% aria-describedby="dwellExtrasHint"></div>
      <div class="row"><label for="wbBeadDwell">Between-bead dwell</label><div class="number-unit"><input id="wbBeadDwell" type="number" value="0" %%wbBeadDwell%% aria-describedby="dwellExtrasHint"><span>s</span></div></div>
      <p class="help" id="dwellExtrasHint">Dwells over 655.35 s run as several TIMERs. A temperature check (0 = off) pauses at the hover for a reading; press START to go on. The between-bead dwell (0 = off) waits at the lift between beads of one layer of a multi-bead wall, only where the travel rises at least 5 mm; other gaps get no dwell and the job notes how many.</p>
      <div class="row"><label for="wbMeasureEvery">Measure stop every N layers</label><input id="wbMeasureEvery" type="number" value="0" %%wbMeasureEvery%% title="0 or blank = off" aria-describedby="measureHint"></div>
      <div class="row"><label for="wbMeasureTimes">Reading times after weld</label><div class="number-unit"><input id="wbMeasureTimes" type="text" value="20, 40, 60" autocomplete="off" spellcheck="false" aria-describedby="measureHint"><span>s</span></div></div>
      <div class="row"><label for="wbMeasurePause">Pause to measure the bead</label><input id="wbMeasurePause" type="checkbox" checked aria-describedby="measureHint"></div>
      <div class="row"><label for="wbMeasureClean">Clean wire (CLEAN_R1) at each stop</label><input id="wbMeasureClean" type="checkbox" aria-describedby="measureHint"></div>
      <div class="row"><label for="wbMeasurePreWeld">Pause for pre-weld temperature</label><input id="wbMeasurePreWeld" type="checkbox" checked aria-describedby="measureHint"></div>
      <p class="help" id="measureHint">At each stop the arm parks clear of the part; read the IR gun as pendant prompts change at the chosen seconds after park arrival, without pausing. Measure and pre-weld pauses wait for START, interpass dwell counts from park arrival, and readings go on the <a data-run-sheet href="/run-sheet" target="_blank" rel="noopener">Run data sheet</a>.</p>
      <p class="help" id="interpassSummary" role="status" aria-live="polite">With the arc off, the torch waits 50 mm above the next layer's start after every layer.</p>
      <details class="accordion" id="thermalPlanPanel"><summary>Thermal plan</summary>
        <div class="row"><label for="wbThermalTarget">Target pre-weld temperature</label><input id="wbThermalTarget" type="text" inputmode="decimal" value="150" data-error="Enter a valid target pre-weld temperature"><select id="wbThermalUnit" aria-label="Thermal plan temperature unit"><option value="C" selected>Â°C</option><option value="F">Â°F</option></select></div>
        <div class="row"><label for="wbThermalPeak">Max peak (optional)</label><input id="wbThermalPeak" type="text" inputmode="decimal" data-error="Enter a valid maximum peak temperature"></div>
        <div class="row"><label for="wbThermalAmbient">Ambient</label><input id="wbThermalAmbient" type="text" inputmode="decimal" value="30" data-error="Enter a valid ambient temperature"></div>
        <div class="row"><label for="wbThermalModel">Model</label><select id="wbThermalModel"><option value="default">Default run-001 fit</option><option value="run">Fit this run</option></select></div>
        <button id="wbThermalCompute" type="button" class="full" disabled>Compute dwells</button>
        <p id="thermalPlanSummary" class="help" role="status" aria-live="polite"></p>
        <table id="thermalPlanTable" hidden><thead><tr><th>Layer</th><th>Start</th><th>Peak</th><th>Pre-weld next</th></tr></thead><tbody id="thermalPlanRows"></tbody></table>
        <p id="thermalPlanNotes" class="help"></p>
        <button id="wbThermalApply" type="button" class="full" hidden disabled>Apply</button>
        <p class="help">Predicted from a lumped fit of surface readings; refit after hardware changes (plate cooler); always verify with the pyrometer.</p>
      </details>
      <div class="row"><label for="wbSmooth">Smooth motion</label><input id="wbSmooth" type="checkbox" checked aria-describedby="smoothHint"></div>
      <p class="help" id="smoothHint">Curved passes run as true arcs (MOVC) and straight runs blend through their split points (PL=1). Untick to write one straight move per segment, as before.</p>
      <div id="autoOptions" hidden>
        <div class="row"><label for="wbAutoSpacing">Touch spacing</label><div class="number-unit"><input id="wbAutoSpacing" type="number" value="10" %%wbAutoSpacing%% data-auto-input="1" disabled><span>mm</span></div></div>
        <div class="row"><label for="wbAutoMax">Max interpass (optional)</label><input id="wbAutoMax" type="number" %%wbAutoMax%% data-auto-input="1" aria-describedby="autoMaxHint" disabled><select id="wbAutoMaxUnit" aria-label="Maximum interpass temperature unit" disabled><option value="F">°F</option><option value="C">°C</option></select></div>
        <p class="help" id="autoMaxHint">Prefilled from the filler as a literature starting value to confirm on this cell: 150 °C for mild steel (Teixeira et al. 2023, for consistent wall geometry), 100 °C for 308L/316L (Penot et al. 2023; Shunmugesh et al. 2025, less sigma phase). Other fillers have no default. Edit it, or clear it for no hold (a cleared field stays cleared).</p>
        <div class="row"><label for="wbAutoAmbient">Ambient (cooling fit)</label><input id="wbAutoAmbient" type="number" %%wbAutoAmbient%% data-auto-input="1" disabled></div>
      </div>
      <button id="emitBtn" disabled class="primary full" style="margin-top:16px">Export robot job · .JBI</button>
      <button id="emitRosBtn" disabled class="full" style="margin-top:10px">Export ROS 2 · air moves</button>
      <p class="help" style="margin-top:8px">ROS 2 exports use arc-off motion. Robot exports require clearance and a physical platter check.</p>
      <details class="accordion" id="autoPanel" open hidden><summary>Auto cycle</summary>
        <p id="autoNext" class="help" role="status" aria-live="polite"></p>
        <p id="autoWarning" class="help"></p><p id="autoSteps" class="help"></p>
        <div class="row"><label for="wbAutoBefore">T-before</label><input id="wbAutoBefore" type="number" %%wbAutoBefore%% data-auto-input="1" disabled></div>
        <div class="row"><label for="wbAutoAfter">T-after</label><input id="wbAutoAfter" type="number" %%wbAutoAfter%% data-auto-input="1" disabled><select id="wbAutoUnit" aria-label="Cycle temperature unit" disabled><option value="F">°F</option><option value="C">°C</option></select></div>
        <button id="wbAutoVarBtn" type="button" class="full" disabled>Import cycle VAR.DAT (P120…)</button><input id="wbAutoVarFile" type="file" accept=".DAT,.dat,.txt" aria-label="Auto cycle VAR.DAT export file" hidden disabled>
        <p id="autoFit" class="help"></p>
        <p id="autoCooling" class="help" role="status" aria-live="polite"></p>
        <button id="wbAutoUseDwell" type="button" class="full" hidden disabled>Use proposed dwell</button>
        <div id="autoHold" hidden><p id="autoHoldMessage" class="help" role="alert"></p>
          <div class="row"><label for="wbAutoReading">Record reading</label><input id="wbAutoReading" type="number" %%wbAutoReading%% data-auto-input="1" disabled><select id="wbAutoReadingUnit" aria-label="New temperature reading unit" disabled><option value="F">°F</option><option value="C">°C</option></select></div>
          <button id="wbAutoReadingBtn" type="button" class="full" disabled>Record reading</button>
        </div>
        <p id="autoStatus" class="help" role="status" aria-live="polite"></p>
      </details>
      <details class="accordion" id="layerSensePanel"><summary>Print in stages · touch a layer</summary>
        <p class="help" id="autoManualHint" hidden>Auto prints in cycles; switch to Manual to touch layers by hand.</p>
        <fieldset id="wbManualStages" style="border:0;margin:0;padding:0;min-width:0">
        <p class="help">Print some layers, touch the top of the last one printed, then print the rest seated on what the touches measured. A touch job is review-gated like any robot job: complete the two-person dry run before running it.</p>
        <label for="wbPrintLayers" class="field-label">Layers to print</label><input id="wbPrintLayers" type="text" inputmode="numeric" autocomplete="off" placeholder="All layers, or a range such as 0-3" class="full" aria-describedby="printLayersHint">
        <p class="help" id="printLayersHint">Both exports above print only these layers. Clearance still checks the whole part, printed layers included.</p>
        <div class="row"><label for="wbSenseLayer">Layer to touch</label><input id="wbSenseLayer" type="number" value="0" %%wbSenseLayer%% disabled></div>
        <div class="row"><label for="wbLayerSpacing">Touch spacing</label><div class="number-unit"><input id="wbLayerSpacing" type="number" value="10" %%wbLayerSpacing%% disabled><span>mm</span></div></div>
        <div class="row"><label for="wbLayerSenseName">Touch job name</label><input id="wbLayerSenseName" type="text" value="LAYER_TOUCH" autocomplete="off" disabled></div>
        <button id="wbLayerSenseJobBtn" type="button" class="full" disabled>Download layer touch job</button>
        <div class="row"><span>VAR.DAT</span><button id="wbLayerVarDatBtn" type="button" disabled>Import layer touches (P120…)</button><input id="wbLayerVarDatFile" type="file" accept=".DAT,.dat,.txt" aria-label="Layer touches VAR.DAT export file" hidden disabled></div>
        <button id="wbLayerSenseClearBtn" type="button" class="full" disabled>Remove the last touch set</button>
        <p class="help" id="layerSenseStatus" role="status" aria-live="polite"></p>
        <p class="help" id="layerSenseReadout">No layer touched. Every layer is seated on the model.</p>
        <input id="wbLayerSense" type="hidden" value="" aria-label="Accepted layer touch sets">
        </fieldset>
      </details>
      <details class="accordion" id="runPanel"><summary>Run log · OneDrive</summary>
        <p class="help"><a data-run-sheet href="/run-sheet" target="_blank" rel="noopener">Run data sheet</a> · Layer and pass temperatures, bead height and width.</p>
        <label for="wbRunName" class="field-label">Run name</label><input id="wbRunName" type="text" maxlength="40" placeholder="Defaults to the job name" autocomplete="off" class="full" disabled>
        <p id="runStatus" class="help" role="status" aria-live="polite">Loading run log…</p>
        <button id="wbRunAddBtn" type="button" class="full" disabled>Add files to the run</button><input id="wbRunFiles" type="file" multiple accept=".jpg,.jpeg,.png,.heic,.webp,.gif,.pdf,.txt,.csv,.xlsx,.xls,.docx,.md,.json,.mp4,.mov,.dat,.jbi,.zip" aria-label="Files to add to the run" hidden disabled>
        <h4>Temperature</h4>
        <div class="row"><label for="wbRunTemperature">Temperature</label><input id="wbRunTemperature" type="number" %%wbRunTemperature%% disabled><select id="wbRunUnit" aria-label="Temperature unit" disabled><option value="F">°F</option><option value="C">°C</option></select></div>
        <label for="wbRunStage" class="field-label">Stage</label><select id="wbRunStage" class="full" disabled><option value="before-interpass">Before interpass</option><option value="after-interpass">After interpass</option><option value="before-weld">Before weld</option><option value="after-weld">After weld</option><option value="other">Other</option></select>
        <label for="wbRunTemperatureNote" class="field-label">Temperature note (optional)</label><input id="wbRunTemperatureNote" type="text" maxlength="200" class="full" disabled>
        <button id="wbRunTemperatureBtn" type="button" class="full" disabled>Record</button>
        <h4>Notes</h4>
        <label for="wbRunNote" class="field-label">Note (time-stamped when added)</label><input id="wbRunNote" type="text" maxlength="2000" autocomplete="off" class="full" disabled>
        <button id="wbRunNoteBtn" type="button" class="full" disabled>Add note</button>
        <h4>Finish run</h4>
        <label for="wbRunOutcome" class="field-label">Outcome</label><select id="wbRunOutcome" class="full" disabled><option value="completed">Completed</option><option value="stopped">Stopped early</option><option value="not-run">Not run</option></select>
        <label for="wbRunDataClass" class="field-label">Data class</label><select id="wbRunDataClass" class="full" disabled><option value="preliminary">Preliminary</option><option value="controlled">Controlled</option></select>
        <div class="row"><label for="wbRunHeight">Height (optional)</label><div class="number-unit"><input id="wbRunHeight" type="text" inputmode="decimal" disabled><span>mm</span></div></div>
        <div class="row"><label for="wbRunWidth">Width (optional)</label><div class="number-unit"><input id="wbRunWidth" type="text" inputmode="decimal" disabled><span>mm</span></div></div>
        <label for="wbRunNotes" class="field-label">Notes</label><textarea id="wbRunNotes" rows="3" maxlength="2000" class="full" disabled></textarea>
        <label for="wbRunArcmon" class="field-label">Arc monitor files</label><input id="wbRunArcmon" type="file" multiple accept=".CSV,.DAT" class="full" disabled>
        <p id="runArcmonStatus" class="help"></p>
        <div id="runArcmonPasses" class="help"></div>
        <label for="wbRunWaiver" class="attestation"><input id="wbRunWaiver" type="checkbox" disabled><span>Arc monitor export not set up yet (record a waiver)</span></label>
        <button id="wbRunFinishBtn" type="button" class="full" disabled>Finish run</button>
        <p id="runMessage" class="help" role="status" aria-live="polite"></p>
        <h4>Recent runs</h4><div id="runRecent" class="help"></div>
      </details>
      <details class="accordion" id="reindexPanel"><summary>Re-index ManPro workholding</summary>
        <p class="help">The class job is never edited: this writes a copy that follows the block. Two-person review before the class runs it.</p>
        <h4>Profile</h4>
        <label for="wbReindexProfileName" class="field-label">Profile name</label><input id="wbReindexProfileName" type="text" value="MANPRO" autocomplete="off" class="full" disabled>
        <label for="wbReindexProfileList" class="field-label">Saved profiles</label><select id="wbReindexProfileList" class="full" disabled><option value="">Choose a profile</option></select>
        <div class="row"><button id="wbReindexLoadBtn" type="button" disabled>Load profile</button><button id="wbReindexRefreshBtn" type="button" disabled>Refresh profiles</button></div>
        <div class="row"><button id="wbReindexUploadBtn" type="button" disabled>Upload profile (.json)</button><input id="wbReindexUploadFile" type="file" accept=".json" aria-label="Re-index profile JSON file" hidden disabled></div>
        <h4>Setup</h4>
        <p class="help">With the block where MANPRO works, freshly cut the wire with CLEAN_R1, use TOOL 0 and check S1 at 0.0° on the pendant. Teach T1–T4 on top, S1–S2 on one long side and E1–E2 on the adjacent end, in P040–P047. Spread the four top contacts near separate corners so any three are noncollinear. Keep top contacts at least 8 mm from the datum edges, side contacts at least 20 mm apart and end contacts at least 10 mm apart. Touch the side/end faces 2–4 mm below the top edge, clear of clamps, coupons and spatter.</p>
        <div class="row"><button id="wbReindexSetupBtn" type="button" disabled>Import setup touches (P040…P047)</button><input id="wbReindexSetupFile" type="file" accept=".DAT,.dat,.txt" aria-label="Setup touches VAR.DAT export file" hidden disabled></div>
        <div class="row"><label for="wbReindexClear">Clear height</label><div class="number-unit"><input id="wbReindexClear" type="number" value="50" %%wbReindexClear%% disabled><span>mm</span></div></div>
        <p class="help">Set the clear height to the tallest thing above the block's top face, plus 20 mm. Changing the setup or clear height clears the reference. Importing setup or reference touches clears the class job; upload the job exported with the block in that position.</p>
        <p class="help" id="reindexSetupReadout">No setup touches imported.</p>
        <label for="wbReindexTouchName" class="field-label">Touch job name</label><input id="wbReindexTouchName" type="text" value="REIDX_TOUCH" maxlength="32" autocomplete="off" class="full" disabled>
        <button id="wbReindexTouchBtn" type="button" class="full" disabled>Download touch job</button>
        <p class="help">S1 at 0.0°, CLEAN_R1, then a two-person dry run: arc off, slow, hand on the E-stop. The eight contacts land in P044–P051.</p>
        <h4>Reference</h4>
        <div class="row"><button id="wbReindexRefBtn" type="button" disabled>Import reference touches (P044…P051)</button><input id="wbReindexRefFile" type="file" accept=".DAT,.dat,.txt" aria-label="Reference touches VAR.DAT export file" hidden disabled></div>
        <h4>Class job</h4>
        <p class="help">Export the class job from the pendant now, while it matches the reference block position.</p>
        <div class="row"><button id="wbReindexSourceBtn" type="button" disabled>Upload class job (.JBI)</button><input id="wbReindexSourceFile" type="file" accept=".JBI,.jbi" aria-label="Original class robot job file" hidden disabled></div>
        <p class="help" id="reindexSourceReadout">No class job uploaded.</p>
        <div class="row"><button id="wbReindexSaveBtn" type="button" disabled>Save profile</button><button id="wbReindexDownloadBtn" type="button" disabled>Download profile (.json)</button></div>
        <p class="help">Save the profile on the server and download a local copy after recording the reference and class job.</p>
        <h4>After a swap</h4>
        <p class="help">Put the block on its match marks (within about ±5 mm sideways and ±2° yaw, with the top within ±1.5 mm vertically). Set S1 to 0.0°, run CLEAN_R1 and the same touch job, then export VAR.DAT. Review the fit before downloading the copy.</p>
        <div class="row"><button id="wbReindexNewBtn" type="button" disabled>Import new touches (P044…P051)</button><input id="wbReindexNewFile" type="file" accept=".DAT,.dat,.txt" aria-label="After-swap touches VAR.DAT export file" hidden disabled></div>
        <div class="help" id="reindexReport" style="white-space:pre-line">No after-swap touches previewed.</div>
        <label for="wbReindexJobName" class="field-label">Re-indexed job name</label><input id="wbReindexJobName" type="text" maxlength="32" autocomplete="off" placeholder="MANPRO_RX" class="full" disabled>
        <button id="wbReindexEmitBtn" type="button" class="full" disabled>Download re-indexed job</button>
        <p class="help" id="reindexStatus" role="status" aria-live="polite"></p>
      </details>
    </section>
    <div class="panel-action"><a download="part.gcode" class="button full" aria-disabled="true" style="display:block;text-align:center;opacity:.5;pointer-events:none">Download G-code</a><p>G-code requires conversion before use on the robot.</p></div>
  </div>
  <div id="wbMsg" role="status" aria-live="polite"></div>
  <dialog id="platterDlg" aria-labelledby="platterTitle"><form method="dialog" id="platterForm" novalidate>
    <h2 id="platterTitle">Check the physical platter</h2><p class="help">Confirm the setup before exporting the robot job.</p>
    <label class="attestation" for="pdClear"><input type="checkbox" id="pdClear" required><span>I looked at the platter. It is clear of anything <strong>not declared below</strong>.</span></label>
    <div><label class="field-label" for="pdBlock">Training block on the platter</label><select id="pdBlock"><option value="1">Yes, on platter</option><option value="0">No, removed</option></select></div>
    <div><label class="field-label" for="pdFixture">Fixture mount on the platter</label><select id="pdFixture"><option value="">None · bare platter</option></select></div>
    <p class="help">Only calibrated fixtures are available.</p>
    <label class="attestation" for="pdAsf" id="pdAsfRow" hidden><input type="checkbox" id="pdAsf"><span>Each listed ASF/AEF exists on the pendant: <strong id="pdAsfList"></strong></span></label>
    <div><label class="field-label" for="pdBy">Checked by</label><input id="pdBy" type="text" maxlength="40" required autocomplete="name"></div>
    <div id="pdErrors" class="field-error" role="alert"></div>
    <div class="button-row"><button value="cancel" type="button" id="pdCancel">Cancel</button><button value="ok" type="submit" class="primary">Confirm &amp; export</button></div>
  </form></dialog>
</div></aside>
</div>
<dialog id="settingsDlg" aria-labelledby="settingsTitle" aria-describedby="settingsDescription">
  <div class="settings-heading"><div><h2 id="settingsTitle">Settings</h2><p id="settingsDescription">Your workspace, your preferences.</p></div><button type="button" id="settingsBack">Back to build</button></div>
  <div class="settings-layout">
    <nav class="settings-nav" role="tablist" aria-label="Settings categories" aria-orientation="vertical">
      <span class="settings-eyebrow">Personal</span>
      <button type="button" id="settings-tab-appearance" role="tab" data-settings-tab="appearance" aria-controls="settings-appearance" aria-selected="true">Appearance</button>
      <button type="button" id="settings-tab-preview" role="tab" data-settings-tab="preview" aria-controls="settings-preview" aria-selected="false" tabindex="-1">3D preview</button>
      <button type="button" id="settings-tab-account" role="tab" data-settings-tab="account" aria-controls="settings-account" aria-selected="false" tabindex="-1">Account</button>
      <button type="button" id="settings-tab-desktop" role="tab" data-settings-tab="desktop" aria-controls="settings-desktop" aria-selected="false" tabindex="-1">Desktop app</button>
      <p id="settingsScope" class="settings-scope">Checking your session…</p>
    </nav>
    <div class="settings-main">
      <div class="settings-content">
        <section id="settings-appearance" role="tabpanel" aria-labelledby="settings-tab-appearance">
          <h3>Appearance</h3><p class="settings-intro">Choose how Studio looks and how much fits on screen.</p>
          <h4>Theme</h4>
          <div class="settings-themes" role="group" aria-label="Workspace theme">
            <button type="button" data-settings-theme="graphite" data-preference-control aria-pressed="true" disabled><span class="settings-theme-window" data-theme="graphite" aria-hidden="true"><span class="settings-theme-head"></span><span class="settings-theme-body"><span class="settings-theme-view"></span><span class="settings-theme-panel"></span></span></span><span class="settings-theme-name">Graphite Studio<span class="settings-theme-check" aria-hidden="true">✓</span></span><span class="settings-theme-note">Default · dark</span></button>
            <button type="button" data-settings-theme="drafting" data-preference-control aria-pressed="false" disabled><span class="settings-theme-window" data-theme="drafting" aria-hidden="true"><span class="settings-theme-head"></span><span class="settings-theme-body"><span class="settings-theme-view"></span><span class="settings-theme-panel"></span></span></span><span class="settings-theme-name">Drafting Room<span class="settings-theme-check" aria-hidden="true">✓</span></span><span class="settings-theme-note">Light panels</span></button>
            <button type="button" data-settings-theme="operator" data-preference-control aria-pressed="false" disabled><span class="settings-theme-window" data-theme="operator" aria-hidden="true"><span class="settings-theme-head"></span><span class="settings-theme-body"><span class="settings-theme-view"></span><span class="settings-theme-panel"></span></span></span><span class="settings-theme-name">Operator Console<span class="settings-theme-check" aria-hidden="true">✓</span></span><span class="settings-theme-note">Dark · utilitarian</span></button>
          </div>
          <div class="settings-row"><div><label for="settingsDensity">Interface density</label><p>Spacing in toolbars and the build inspector.</p></div><select id="settingsDensity" data-preference-control disabled><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></div>
          <div id="settingsDensityPreview" class="settings-density-preview" data-density="comfortable" aria-label="Selected density preview"><span>Layer height</span><span>2.9 mm</span></div>
          <div class="settings-row"><div><label for="settingsReduceMotion">Reduce motion</label><p>Use Next layer instead of continuous toolpath playback.</p></div><input id="settingsReduceMotion" type="checkbox" data-preference-control disabled></div>
        </section>
        <section id="settings-preview" role="tabpanel" aria-labelledby="settings-tab-preview" hidden>
          <h3>3D preview</h3><p class="settings-intro">Defaults for new views. Applying a changed option also updates the current preview.</p>
          <div class="settings-row"><div><label for="settingsDefaultFrame">Default framing</label><p>The full cell or a closer view of the part.</p></div><select id="settingsDefaultFrame" data-preference-control disabled><option value="cell">Cell view</option><option value="part">Part view</option></select></div>
          <div class="settings-row"><div><label for="settingsDefaultCamera">Default camera</label><p>Starting angle for the preview.</p></div><select id="settingsDefaultCamera" data-preference-control disabled><option value="iso">Isometric</option><option value="top">Top</option></select></div>
          <div class="settings-row"><div><label for="settingsPathOnly">Path only</label><p>Show the toolpath without the cell, work surface, or part model.</p></div><input id="settingsPathOnly" type="checkbox" data-preference-control disabled></div>
          <p class="settings-note">Cell setup stays in Prepare and Place. Display preferences do not change placement, clearance checks, or robot exports.</p>
        </section>
        <section id="settings-account" role="tabpanel" aria-labelledby="settings-tab-account" hidden>
          <h3>Account</h3><p class="settings-intro">Your identity and sign-in methods.</p>
          <div id="settingsIdentity" class="settings-account-identity">Checking your session…</div>
          <p id="settingsAccountNote" class="settings-account-note">Account information comes from your current session.</p>
          <div id="settingsAuthActions" class="settings-auth-actions"></div>
          <div id="settingsPasskeys"></div>
        </section>
        <section id="settings-desktop" role="tabpanel" aria-labelledby="settings-tab-desktop" hidden>
          <h3>Desktop app</h3><p class="settings-intro">Download WAAM Studio for a Mac with Apple silicon or a Windows PC.</p>
          <p id="settingsDesktopState" class="settings-desktop-state" role="status" aria-live="polite">Open this tab to check for a desktop build.</p>
          <button type="button" id="settingsDesktopRetry" hidden>Try again</button>
          <div id="settingsDesktopRelease" hidden>
            <article class="settings-desktop-platform" data-desktop-platform="macos-arm64" hidden>
              <h4>Mac</h4>
              <dl class="settings-desktop-details">
                <div><dt>Version</dt><dd data-desktop-field="version"></dd></div>
                <div><dt>Platform</dt><dd data-desktop-field="platform"></dd></div>
                <div><dt>Size</dt><dd data-desktop-field="size"></dd></div>
                <div><dt>Published</dt><dd><time data-desktop-field="published"></time></dd></div>
                <div><dt>Build trust</dt><dd data-desktop-field="trust"></dd></div>
                <div><dt>Release signature</dt><dd data-desktop-field="signature"></dd></div>
              </dl>
              <div class="settings-desktop-sha"><div><span>SHA-256</span><code data-desktop-field="sha"></code></div><button type="button" data-desktop-copy disabled>Copy</button></div>
              <p class="settings-desktop-notes" data-desktop-field="notes" hidden></p>
              <a class="button primary settings-desktop-download" data-desktop-field="download" download>Download for Mac (Apple Silicon)</a>
              <p class="settings-desktop-platform-note">Apple Silicon (arm64) only. This build does not run on Intel Macs.</p>
              <ol class="settings-desktop-steps">
                <li>Open the DMG, or the ZIP, and drag WAAM Studio into Applications.</li>
                <li>This development build is not notarized, so macOS may block its first launch. Open System Settings › Privacy &amp; Security › “Open Anyway.” Never turn off Gatekeeper.</li>
                <li>WAAM Studio 0.2.0 and later update themselves to signed releases. They check at launch, every 6 hours, and when you choose Help › Check for Updates…, but the first of these versions must be installed by hand once. A build marked “not signed — install manually” never arrives as an update.</li>
              </ol>
            </article>
            <article class="settings-desktop-platform" data-desktop-platform="windows-x64" hidden>
              <h4>Windows</h4>
              <dl class="settings-desktop-details">
                <div><dt>Version</dt><dd data-desktop-field="version"></dd></div>
                <div><dt>Platform</dt><dd data-desktop-field="platform"></dd></div>
                <div><dt>Size</dt><dd data-desktop-field="size"></dd></div>
                <div><dt>Published</dt><dd><time data-desktop-field="published"></time></dd></div>
                <div><dt>Build trust</dt><dd data-desktop-field="trust"></dd></div>
                <div><dt>Release signature</dt><dd data-desktop-field="signature"></dd></div>
              </dl>
              <div class="settings-desktop-sha"><div><span>SHA-256</span><code data-desktop-field="sha"></code></div><button type="button" data-desktop-copy disabled>Copy</button></div>
              <p class="settings-desktop-notes" data-desktop-field="notes" hidden></p>
              <a class="button primary settings-desktop-download" data-desktop-field="download" download>Download for Windows (x64)</a>
              <p class="settings-desktop-platform-note">64-bit Windows 10 or 11. Installs for your Windows account only, without administrator rights.</p>
              <ol class="settings-desktop-steps">
                <li>Before running it, compare the installer’s SHA-256 with the value above: <code>Get-FileHash "$HOME\\Downloads\\*WAAM-Studio-*-setup.exe"</code>. The saved name starts with part of its SHA-256.</li>
                <li>This development installer is not code-signed, so SmartScreen may warn. Only when the SHA-256 matches, choose More info › “Run anyway.”</li>
                <li>WAAM Studio 0.2.0 and later update themselves to signed releases. They check at launch, every 6 hours, and when you choose Help › Check for Updates…, but the first of these versions must be installed by hand once. A build marked “not signed — install manually” never arrives as an update.</li>
              </ol>
            </article>
            <div class="settings-desktop-install">
              <h4>Connect</h4>
              <p>In WAAM Studio, create a project and enter this server address: <code id="settingsDesktopOrigin"></code>. Do not add a token.</p>
            </div>
          </div>
        </section>
      </div>
      <div class="settings-actions"><div class="settings-feedback"><p id="settingsStatus" role="status" aria-live="polite">Checking your session…</p><button type="button" id="settingsRetry" hidden>Retry</button></div><button type="button" id="settingsCancel" disabled>Cancel changes</button><button type="button" id="settingsApply" class="primary" disabled>Apply preferences</button></div>
    </div>
  </div>
</dialog>
<dialog id="helpDlg" aria-labelledby="helpTitle">
  <div class="help-heading"><h2 id="helpTitle">Using WAAM Studio</h2><button id="helpClose" type="button" aria-label="Close help">Close</button></div>
  <nav class="help-tabs" role="tablist" aria-label="Help topics">
    <button id="help-tab-start" type="button" role="tab" aria-selected="true" aria-controls="help-start" data-help-topic="start">Quick start</button>
    <button id="help-tab-controls" type="button" role="tab" aria-selected="false" tabindex="-1" aria-controls="help-controls" data-help-topic="controls">Controls</button>
    <button id="help-tab-troubleshoot" type="button" role="tab" aria-selected="false" tabindex="-1" aria-controls="help-troubleshoot" data-help-topic="troubleshoot">Troubleshooting</button>
  </nav>
  <div class="help-body">
    <div id="help-start" role="tabpanel" aria-labelledby="help-tab-start">
      <p id="helpProgress" class="help-progress" role="status" aria-live="polite"></p>
      <h3 id="helpStepTitle" tabindex="-1"></h3>
      <ol id="helpSteps"></ol>
      <p id="helpContext" class="help-context"></p>
    </div>
    <div id="help-controls" role="tabpanel" aria-labelledby="help-tab-controls" hidden>
      <h3>Navigate the preview</h3>
      <dl class="help-reference">
        <dt>Orbit</dt><dd>Drag an empty area of the preview.</dd>
        <dt>Pan</dt><dd>Hold <kbd>Shift</kbd> and drag, or drag with the middle mouse button. The orbit center moves with the view.</dd>
        <dt>Projection / travel</dt><dd>Click Ortho beside the camera presets to switch to Persp. In Persp, scroll up to move forward through surfaces and down to move back. Hold <kbd>Shift</kbd> while scrolling for fine travel. Click Persp to return to Ortho.</dd>
        <dt>Zoom / reset</dt><dd>In Ortho, scroll the mouse wheel to zoom. Fit view resets pan and travel; <kbd>F</kbd> frames the part or cell.</dd>
        <dt>Frame the model</dt><dd>Use Cell view, Part view, or Fit view.</dd>
        <dt>Window layout</dt><dd>Drag the build panel's left edge to resize it, or use Layout for its width and text size. <kbd>&#92;</kbd> hides the panel so the preview fills the window; any step tab brings it back. Saved in this browser.</dd>
        <dt>Scale bars</dt><dd>On Place, rulers run along the platter from the faceplate end and the robot-side edge, or along a measured substrate from corner 1 (edge 1→2, then square off it). The dashed lines are the two tape measurements to the weld path's nearest corner; the same numbers are listed under Part position. Turn them off in Layers.</dd>
        <dt>Re-slice</dt><dd>On Prepare, press <kbd>Enter</kbd> in a number field or <kbd>Ctrl</kbd>+<kbd>Enter</kbd> anywhere to run Slice &amp; check.</dd>
        <dt>Standard views</dt><dd>Use Iso, Top, Front, or Right.</dd>
        <dt>Playback</dt><dd>Use Play, the timeline, and the speed selector.</dd>
        <dt>Fume hood</dt><dd>Use Layers to hide the extraction assembly when it is moved aside. This setting changes the preview only and is remembered in this browser.</dd>
      </dl>
      <h4>Keyboard shortcuts</h4>
      <p class="help">Click the preview before using these shortcuts.</p>
      <dl class="help-reference">
        <dt><kbd>Space</kbd></dt><dd>Play or pause the toolpath.</dd>
        <dt><kbd>←</kbd> <kbd>→</kbd></dt><dd>Step through toolpath moves.</dd>
        <dt><kbd>F</kbd></dt><dd>Switch between part and cell framing.</dd>
        <dt><kbd>Esc</kbd></dt><dd>Cancel a placement preview or close Help.</dd>
        <dt><kbd>Alt</kbd> + drag</dt><dd>Move a placement handle without snapping.</dd>
      </dl>
      <h4>Placement settings</h4>
      <dl class="help-reference">
        <dt>X and Y offsets</dt><dd>Move the part from its default position.</dd>
        <dt>Z rotation</dt><dd>Rotate the part on the work surface.</dd>
        <dt>Substrate thickness</dt><dd>Thickness of the plate beneath the part.</dd>
        <dt>Lift-off gap</dt><dd>Additional height above the substrate. Snap to substrate sets this to zero.</dd>
        <dt>Bead spacing</dt><dd>ORNL pass spacing as a percentage of bead width.</dd>
      </dl>
    </div>
    <div id="help-troubleshoot" role="tabpanel" aria-labelledby="help-tab-troubleshoot" hidden>
      <details open><summary>The part is the wrong size</summary><p>Compare the displayed dimensions with your CAD model. Change STL units in Prepare if needed, then slice again. STL files do not store units.</p></details>
      <details><summary>The slice fails</summary><p>Read the message below the Slice button. Check the highlighted inputs and model units. ORNL requires a closed solid STL; Built-in follows thin-wall contours.</p></details>
      <details><summary>Robot export is unavailable</summary><p>Sign in, resolve blocking clearance findings, and check the current placement. HC10DTP supports motion preview. Its robot exports require cell, tool, and controller calibration.</p></details>
      <details><summary>A collision may be a model artifact</summary><p>Negative clearance indicates penetration. Purple and blue mark possible model artifacts; red and amber mark other penetration and proximity findings. Artifact labels do not remove an export block. Inspect the location and model geometry.</p></details>
      <details><summary>How closely does the model match the cell?</summary><p>The models combine vendor CAD, cell photos and recorded measurements. Some furniture positions and the hood dimensions remain approximate. Cobot playback checks reachability against nominal cell geometry. Cobot robot export remains unavailable.</p></details>
      <details><summary>The preview is slow or incomplete</summary><p>Open Layers and select Light preview. Select Full detail to retry loading the detailed models. Preview detail does not change the slicing or clearance results.</p></details>
      <details><summary>A part or build is missing</summary><p>Sign in with the account that saved it. Parts are in the Prepare library; saved build settings are in Export. Refresh the library if an upload is missing. Shared links may open a different workspace.</p></details>
      <details><summary>Simulation and robot timing differ</summary><p>Interpass simulation estimates cooling from the selected profile. Example profiles are uncalibrated, and simulated cooling times are not applied to robot exports.</p></details>
    </div>
  </div>
  <div id="helpWalkActions" class="help-actions">
    <button id="helpGo" type="button">Open Prepare</button>
    <div class="help-paging"><button id="helpBack" type="button">Back</button><button id="helpNext" class="primary" type="button">Next</button></div>
  </div>
</dialog>
<script data-renderer>%%RENDERER%%</script>
<script data-library>%%LIBRARY%%</script>
<script data-help>%%HELP%%</script>
<script data-settings>%%SETTINGS%%</script>
<script data-window>%%WINDOW%%</script>
<script data-scale>%%SCALE%%</script>
"""
