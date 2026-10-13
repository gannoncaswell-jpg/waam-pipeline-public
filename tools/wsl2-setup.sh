#!/usr/bin/env bash


set -euo pipefail


PYBULLET_VERSION="${PYBULLET_VERSION:-3.2.7}"
VENV="${WAAM_VENV:-$HOME/.venvs/waam}"
DO_APT=1
DO_GUI=0
DO_ORNL=0


APT_PACKAGES=(
    build-essential
    python3-dev
    python3-venv
    ca-certificates curl git
    libgl1
    libglx-mesa0
    libx11-6
)
ORNL_PACKAGES=(direnv git-lfs)


if [ -z "${BASH_VERSION:-}" ]; then
    echo "wsl2-setup.sh needs bash (run 'bash wsl2-setup.sh', not 'sh')." >&2
    exit 1
fi

_bold=""; _dim=""; _off=""
if [ -t 1 ]; then _bold=$'\033[1m'; _dim=$'\033[2m'; _off=$'\033[0m'; fi

phase()  { printf '\n%s==> %s%s\n' "$_bold" "$*" "$_off"; }
note()   { printf '    %s\n' "$*"; }
detail() { printf '    %s%s%s\n' "$_dim" "$*" "$_off"; }
warn()   { printf '    WARNING: %s\n' "$*" >&2; }
DIED=0
die()    { DIED=1; printf '\nFAILED: %s\n' "$1" >&2; shift; for l in "$@"; do printf '  %s\n' "$l" >&2; done; exit 1; }

usage() {
    cat <<'USAGE'
usage: bash wsl2-setup.sh [options]

  --gui                 open a real PyBullet GUI window at the end (WSLg)
  --no-apt              skip the apt phase (already provisioned, or no sudo)
  --venv PATH           virtualenv location (default: ~/.venvs/waam)
  --pybullet VERSION    pybullet version to install (default: 3.2.7)
  --ornl-prereqs        also install the apt half of ORNLSlicer's setup
  -h, --help            this

Environment: WAAM_VENV, PYBULLET_VERSION override the defaults.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --gui)           DO_GUI=1 ;;
        --no-apt)        DO_APT=0 ;;
        --ornl-prereqs)  DO_ORNL=1 ;;
        --venv)          [ $# -ge 2 ] || die "--venv needs a path"; VENV="$2"; shift ;;
        --pybullet)      [ $# -ge 2 ] || die "--pybullet needs a version"; PYBULLET_VERSION="$2"; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               usage >&2; die "unknown argument: $1" ;;
    esac
    shift
done

FAIL_LINE="?"
trap 'FAIL_LINE=$LINENO' ERR

trap 'rc=$?; if [ $rc -ne 0 ] && [ "$DIED" -eq 0 ]; then printf "\nwsl2-setup.sh aborted (exit %s, near line %s).\nNothing was left running; fix the error above and re-run -- it is idempotent.\n" "$rc" "$FAIL_LINE" >&2; fi' EXIT


phase "1/7  Preflight"

[ "$(uname -s)" = "Linux" ] || die "this runs inside the WSL2 Linux guest, not on Windows." \
    "From PowerShell: wsl" \
    "then: bash <path to this script>"

if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
    note "WSL guest: yes  $(tr -d '\0' </proc/sys/kernel/osrelease 2>/dev/null || echo '?')"
else
    warn "this does not look like WSL. Harmless on plain Ubuntu; the GUI and"
    warn "/mnt/c notes below will not apply."
fi

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
    warn "running as root: the venv will land in /root, not in your user's home."
    warn "Prefer running as the normal user created at first boot."
else
    SUDO="sudo"
fi

command -v apt-get >/dev/null 2>&1 || die "no apt-get: this script targets Debian/Ubuntu." \
    "docs/wsl2-setup.md pins Ubuntu 24.04 for a reason (ROS 2 Jazzy + ORNLSlicer)."

DISTRO="unknown"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    DISTRO="$(. /etc/os-release; echo "${PRETTY_NAME:-$NAME $VERSION_ID}")"
fi
note "Distribution: $DISTRO"
case "$DISTRO" in
    *"24.04"*) : ;;
    *) warn "expected Ubuntu 24.04 LTS. Continuing, but docs/wsl2-setup.md sec. 4"
       warn "explains why 24.04 is the pinned target." ;;
esac

detail "CPUs: $(nproc 2>/dev/null || echo '?')   RAM: $(awk '/MemTotal/{printf "%.1f GiB", $2/1048576}' /proc/meminfo 2>/dev/null || echo '?')"
detail "pybullet compiles at N = min(RAM_in_GB, 2 x CPUs) jobs -- the RAM cap"
detail "usually binds first under WSL2's 50%-of-host default."


phase "2/7  System packages"

if [ "$DO_APT" -eq 0 ]; then
    note "skipped (--no-apt)"
else
    missing=()
    for pkg in "${APT_PACKAGES[@]}"; do
        if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed"; then
            missing+=("$pkg")
        fi
    done

    if [ "${#missing[@]}" -eq 0 ]; then
        note "all present: ${APT_PACKAGES[*]}"
    else
        note "installing: ${missing[*]}"
        if [ -n "$SUDO" ] && ! command -v sudo >/dev/null 2>&1; then
            die "sudo is not installed and you are not root." \
                "Install these by hand, then re-run with --no-apt:" \
                "  ${missing[*]}"
        fi
        $SUDO apt-get update

        $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
    fi

    still_missing=()
    for pkg in "${APT_PACKAGES[@]}"; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed" || still_missing+=("$pkg")
    done
    [ "${#still_missing[@]}" -eq 0 ] || die "apt reported success but these are still not installed: ${still_missing[*]}" \
        "Check the apt output above -- a broken sources.list or no network is the usual cause."
fi

if [ "$DO_ORNL" -eq 1 ]; then
    note "ORNLSlicer prerequisites (apt half only): ${ORNL_PACKAGES[*]}"
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "${ORNL_PACKAGES[@]}"
    git lfs install
    note "The Nix half is NOT scripted: ORNL's guide requires answering 'no' to the"
    note "Determinate Nix prompt and 'yes' to Nix. Run it by hand, then"
    note "'nix develop -L' in an ext4 clone. docs/wsl2-setup.md sec. 10."
fi


phase "3/7  Python interpreter"

command -v python3 >/dev/null 2>&1 || die "no python3 on PATH."
PY_FULL="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
PY_MINOR="$(python3 -c 'import sys; print(sys.version_info[1])')"
PY_MAJOR="$(python3 -c 'import sys; print(sys.version_info[0])')"
note "python3 is $PY_FULL"

[ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 10 ] || die "waam-pipeline requires Python >= 3.10 (pyproject.toml); found $PY_FULL."

if [ "$PY_MINOR" -le 11 ]; then
    note "a cp3${PY_MINOR} manylinux wheel exists -- pip will download it, no compile."
else
    note "pybullet ${PYBULLET_VERSION}'s newest wheel is cp311, so there is NO wheel"
    note "for cp3${PY_MINOR}. pip will build the 80 MB source tarball. This is the"
    note "intended path, not a fallback -- docs/wsl2-setup.md sec. 6."
fi


phase "4/7  Virtualenv at $VENV"

if [ -x "$VENV/bin/python" ]; then
    VENV_PY_VER="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    note "already exists (Python $VENV_PY_VER)"
    if [ "$VENV_PY_VER" != "${PY_MAJOR}.${PY_MINOR}" ]; then
        warn "the venv is on Python $VENV_PY_VER but python3 is now ${PY_MAJOR}.${PY_MINOR}."
        warn "If imports misbehave, delete $VENV and re-run."
    fi
else
    mkdir -p "$(dirname "$VENV")"
    python3 -m venv "$VENV" || die "python3 -m venv failed." \
        "On Ubuntu that usually means python3-venv is missing; re-run without --no-apt."
    note "created"
fi

VPY="$VENV/bin/python"
[ -x "$VPY" ] || die "$VPY is not executable after venv creation."


"$VPY" -m pip install --quiet --upgrade pip setuptools wheel \
    || die "could not bootstrap pip/setuptools/wheel in the venv."
detail "pip $("$VPY" -m pip --version | awk '{print $2}')   setuptools $("$VPY" -c 'import setuptools; print(setuptools.__version__)')"


phase "5/7  Build prerequisites"


if "$VPY" -c 'import distutils.ccompiler' >/dev/null 2>&1; then
    note "distutils shim: present"
else
    warn "distutils is missing -- setuptools has dropped its shim. Trying an older setuptools."
    "$VPY" -m pip install --quiet "setuptools<75" || true
    "$VPY" -c 'import distutils.ccompiler' >/dev/null 2>&1 || die \
        "pybullet ${PYBULLET_VERSION}'s setup.py needs distutils and nothing provides it." \
        "Options, in order of preference:" \
        "  - use a newer pybullet that has a pyproject.toml, or a prebuilt wheel;" \
        "  - pin an older setuptools that still ships _distutils_hack;" \
        "  - build in a Python <= 3.11 venv (but read docs/wsl2-setup.md sec. 6 first)."
    note "distutils shim: restored via older setuptools"
fi


"$VPY" -m pip install --quiet --upgrade numpy || die "could not install numpy into the venv."
note "numpy $("$VPY" -c 'import numpy; print(numpy.__version__)') (installed before pybullet, on purpose)"


phase "6/7  PyBullet $PYBULLET_VERSION"

INSTALLED_VER="$("$VPY" -c 'import importlib.metadata as m; print(m.version("pybullet"))' 2>/dev/null || true)"

if [ "$INSTALLED_VER" = "$PYBULLET_VERSION" ]; then
    note "already installed at $INSTALLED_VER -- skipping"
else
    if [ -n "$INSTALLED_VER" ]; then note "replacing pybullet $INSTALLED_VER"; fi

    PIP_ARGS=(--no-build-isolation "pybullet==$PYBULLET_VERSION")
    if [ "$PY_MINOR" -ge 12 ]; then
        note "compiling 271 C/C++ sources. Expect 5-20 minutes."
        note "-v is on deliberately: without it pip parks on 'Building wheel for"
        note "pybullet' for the whole build and looks hung. Watch for 'numpy is"
        note "enabled.' near the top -- that is the line that matters."
        PIP_ARGS=(-v "${PIP_ARGS[@]}")
    fi
    "$VPY" -m pip install "${PIP_ARGS[@]}" || die \
        "the pybullet install failed." \
        "Most likely causes, in order:" \
        "  - build-essential or python3-dev missing (re-run without --no-apt);" \
        "  - the guest ran out of memory mid-compile -- raise memory in" \
        "    %USERPROFILE%\\.wslconfig, then 'wsl --shutdown', then re-run;" \
        "  - out of disk in the VHDX (needs roughly 4 GB free)." \
        "See docs/wsl2-setup.md sec. 7."
fi


phase "7/7  Verify"

VERIFY_OUT="$("$VPY" - <<'PY'
import importlib.metadata as md
import pybullet as p
import pybullet_data

print("version=%s" % md.version("pybullet"))
print("api=%s" % p.getAPIVersion())
print("module=%s" % p.__file__)

cid = p.connect(p.DIRECT)
assert cid >= 0, "p.connect(p.DIRECT) failed"
p.setAdditionalSearchPath(pybullet_data.getDataPath())
p.setGravity(0, 0, -9.81)
plane = p.loadURDF("plane.urdf")
bot = p.loadURDF("r2d2.urdf", [0, 0, 1])
for _ in range(120):
    p.stepSimulation()
pts = p.getClosestPoints(bodyA=bot, bodyB=plane, distance=2.0)
print("closest_points=%d" % len(pts))
img = p.getCameraImage(32, 24)
print("rgb_type=%s" % type(img[2]).__module__)
p.disconnect()
PY
)" || die "pybullet installed but does not work." \
    "Read the traceback above. If it is 'libGL.so.1: cannot open shared object file'," \
    "re-run without --no-apt to get libgl1/libglx-mesa0/libx11-6."

get() { printf '%s\n' "$VERIFY_OUT" | sed -n "s/^$1=//p"; }

note "pybullet $(get version)   (API $(get api))"
detail "$(get module)"
note "DIRECT server: connected, 120 steps, getClosestPoints returned $(get closest_points) pairs"

if [ "$(get rgb_type)" = "numpy" ]; then
    note "numpy support: ENABLED (getCameraImage returns arrays)"
else
    warn "numpy support: DISABLED -- getCameraImage returns flat sequences."
    warn "pybullet still works; only camera readback is affected. To fix, rebuild --"
    warn "--no-cache-dir matters, or pip reuses the wheel it already built:"
    warn "  $VPY -m pip install --force-reinstall --no-cache-dir \\"
    warn "      --no-build-isolation --no-binary pybullet pybullet==$PYBULLET_VERSION"
fi

if [ "$DO_GUI" -eq 1 ]; then
    phase "GUI smoke test (WSLg)"
    if [ -z "${DISPLAY:-}" ]; then
        warn "DISPLAY is empty -- WSLg is not wired up in this shell."
        warn "Try 'wsl --shutdown' from PowerShell and reopen the shell."
        warn "Not fatal: p.connect(p.DIRECT) does everything T13 needs."
    else
        note "DISPLAY=$DISPLAY -- a window should open for about 5 seconds."
        "$VPY" - <<'PY' || echo "    GUI failed; see docs/wsl2-setup.md sec. 8. DIRECT mode is unaffected."
import time
import pybullet as p
import pybullet_data

p.connect(p.GUI)
p.setAdditionalSearchPath(pybullet_data.getDataPath())
p.setGravity(0, 0, -9.81)
p.loadURDF("plane.urdf")
p.loadURDF("r2d2.urdf", [0, 0, 1])
end = time.time() + 5
while time.time() < end:
    p.stepSimulation()
    time.sleep(1 / 240)
p.disconnect()
PY
    fi
fi


phase "Done"
note "Interpreter:  $VPY"
note "Activate:     source $VENV/bin/activate"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
if [ -f "$REPO/pyproject.toml" ] && [ -d "$REPO/src/waam_pipeline" ]; then
    echo
    note "Repo found at: $REPO"
    note "Run the suite (quote the path -- it has a space in it):"
    note "  cd \"$REPO\" && \"$VPY\" -m unittest discover -s tests"
    note "Build-heavy work does NOT belong on /mnt/c; see docs/wsl2-setup.md sec. 9."
else
    echo
    note "Place this script in the repository tools directory:"
    note "  /path/to/waam-pipeline"
fi

echo
note "Next: docs/wsl2-setup.md sec. 8 (GUI), sec. 10 (ORNLSlicer), sec. 11 (ROS 2 Jazzy)."
