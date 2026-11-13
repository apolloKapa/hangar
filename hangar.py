#!/usr/bin/env python3
"""Hangar: a launcher for the games and apps you have outside Steam.

Add anything you can run: a Windows .exe (run through Proton, GE-Proton by default, or Wine), a
native binary, an AppImage, a script or a plain command such as `flatpak run ...`. Each Windows
program gets its own prefix. Games and apps have separate tabs, each with its library, favourites,
NSFW and hidden lists, shown as a list or a grid of covers. Covers come from the Steam store or
SteamGridDB (with your own API key) or any image file; Windows programs also get their .exe icon.

Everything Hangar knows is in ~/.config/hangar (library.json, settings.json) and
~/.local/share/hangar (covers, icons, logs, prefixes of its own runners). Games keep their own
files where they are.

  hangar                 open the window
  hangar --launch ID     run one entry without the window (used by menu shortcuts)
  hangar FILE.exe        open a Windows program (or --open FILE): a small window to run it, pick
                         Proton/Wine and optionally add it to the library, like PortProton
  hangar --run FILE      run a program right away with the default runner, no window
  hangar --check         list the Proton/Wine versions and tools Hangar finds
"""
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

from PyQt6.QtCore import QRectF, QSize, QSocketNotifier, Qt, QThread, QTimer, QObject, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QImage, QKeySequence, QLinearGradient, QPainter,
                         QPainterPath, QPalette, QPixmap, QShortcut)
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog,
                             QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
                             QKeySequenceEdit, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                             QScrollArea, QSizePolicy, QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget)

VERSION = "1.8.0"
HOME = os.path.expanduser("~")
CONF_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "hangar")
DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local/share"), "hangar")
LIBRARY = os.path.join(CONF_DIR, "library.json")
SETTINGS = os.path.join(CONF_DIR, "settings.json")
COVERS = os.path.join(DATA, "covers")
ICONS = os.path.join(DATA, "icons")
LOGS = os.path.join(DATA, "logs")
RUNNERS = os.path.join(DATA, "runners")
TOOLS = os.path.join(DATA, "tools")
MENU_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local/share"), "applications")
HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "hangar.svg")
WINDOWS_EXT = (".exe", ".msi", ".bat", ".cmd", ".lnk")
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.ico *.svg)"
STEAM_ROOTS = [os.path.join(HOME, ".steam/root"), os.path.join(HOME, ".local/share/Steam"),
               os.path.join(HOME, ".steam/steam"),
               os.path.join(HOME, ".var/app/com.valvesoftware.Steam/.local/share/Steam")]
GE_RELEASES = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest"
HANGAR_RELEASES = "https://api.github.com/repos/apolloKapa/hangar/releases/latest"
HANGAR_PAGE = "https://github.com/apolloKapa/hangar/releases/latest"
DW_RELEASES = "https://dawn.wine/api/v1/repos/dawn-winery/dwproton/releases/latest"
# Protons Hangar can download: key -> (label, latest-release API, archive suffix)
PROTON_SOURCES = {"ge": ("GE-Proton", GE_RELEASES, ".tar.gz"), "dw": ("DWProton", DW_RELEASES, ".tar.xz")}
WINETRICKS_URL = "https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks"
KINDS = {"game": "Games", "app": "Apps"}
TABS = {"library": "Library", "favorites": "Favourites", "vr": "VR", "nsfw": "NSFW", "hidden": "Hidden"}
SORTS = {"name": "Name", "recent": "Last played", "playtime": "Most played", "added": "Recently added"}
RUNNER_NAMES = {"native": "Native", "proton": "Proton", "wine": "Wine"}
SIZES = {"small": 140, "medium": 172, "large": 214}

# The AppImage's own environment, read before host_env() hides it from games.
APPIMAGE = os.environ.get("APPIMAGE")


# ---- storage -----------------------------------------------------------------------------------

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
    os.replace(tmp, path)


DEFAULT_SETTINGS = {
    "theme": "Midnight", "accent": "", "view": "grid", "size": "medium", "kind": "game", "tab": "library",
    "sort": "name", "proton": "", "wine": "", "windows_runner": "proton",
    "prefix_root": os.path.join(HOME, "Games", "Hangar", "prefixes"), "use_umu": True,
    "show_nsfw": False, "blur_nsfw": True, "sgdb_key": "", "vr_options": "", "start_wivrn": True, "wayland": True, "games_root": os.path.join(HOME, "Games"),
    "exe_opener": False, "close_to_tray": True,
    # NSFW privacy: hide the tab (a key combination shows it), and a password to open it
    "nsfw_hidden": False, "nsfw_key": "Ctrl+Shift+N", "nsfw_pass": {},
}


def hash_password(pw, salt=None):
    """{"salt", "hash"}: scrypt (PBKDF2 where OpenSSL has no scrypt); the password itself isn't kept"""
    salt = salt or os.urandom(16)
    try:
        h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
        kind = "scrypt"
    except (AttributeError, ValueError):
        h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 300_000)
        kind = "pbkdf2"
    return {"kind": kind, "salt": salt.hex(), "hash": h.hex()}


def password_ok(pw, rec):
    import hmac
    if not rec or not rec.get("hash"):
        return True
    try:
        salt = bytes.fromhex(rec["salt"])
    except (KeyError, ValueError):
        return False
    if rec.get("kind") == "pbkdf2":
        h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 300_000)
    else:
        try:
            h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
        except (AttributeError, ValueError):
            return False
    return hmac.compare_digest(h.hex(), rec["hash"])


def settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(read_json(SETTINGS, {}))
    return s


def save_settings(**changes):
    s = read_json(SETTINGS, {})
    s.update(changes)
    write_json(SETTINGS, s)


ENTRY_DEFAULTS = {
    "id": "", "name": "", "kind": "game", "exe": "", "args": "", "cwd": "", "runner": "native", "version": "",
    "prefix": "", "env": "", "wrapper": "", "mangohud": False, "gamemode": False, "favorite": False,
    "nsfw": False, "hidden": False, "vr": False, "eac": False, "battleye": False, "cover": "", "icon": "", "added": 0, "last_played": 0, "playtime": 0,
}


def library():
    out = []
    for e in read_json(LIBRARY, []):
        if isinstance(e, dict) and e.get("id"):
            d = dict(ENTRY_DEFAULTS)
            d.update(e)
            out.append(d)
    return out


def get_entry(eid):
    return next((e for e in library() if e["id"] == eid), None)


def put_entry(entry):
    """adds the entry, or replaces the one with its id"""
    lib = library()
    for i, e in enumerate(lib):
        if e["id"] == entry["id"]:
            lib[i] = entry
            break
    else:
        lib.append(entry)
    write_json(LIBRARY, lib)


def update_entry(eid, **changes):
    lib = library()
    for e in lib:
        if e["id"] == eid:
            e.update(changes)
            write_json(LIBRARY, lib)
            return e
    return None


def remove_entry(eid):
    write_json(LIBRARY, [e for e in library() if e["id"] != eid])


def add_playtime(eid, seconds):
    e = get_entry(eid)
    if e:
        update_entry(eid, playtime=int(e["playtime"]) + max(0, int(seconds)), last_played=int(time.time()))


def new_id():
    return uuid.uuid4().hex[:12]


def slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "game"


def guess_name(path):
    """a name from an executable's path: its file name, or its folder's for generic names"""
    base = os.path.splitext(os.path.basename(path))[0]
    generic = {"game", "launcher", "start", "play", "setup", "run", "main", "app", "client", "win64", "bin",
               "binaries", "x64", "shipping"}
    words = re.sub(r"[-_.]+", " ", base).strip()
    parent = path
    if re.sub(r"[^a-z0-9]", "", base.lower()) in generic or base.lower().endswith("-win64-shipping"):
        for _ in range(4):
            parent = os.path.dirname(parent)
            pname = os.path.basename(parent)
            if pname and re.sub(r"[^a-z0-9]", "", pname.lower()) not in generic:
                words = re.sub(r"[-_.]+", " ", pname).strip()
                break
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", words)
    words = re.sub(r"\b(x64|x86|x86 64|win64|win32|appimage|amd64|linux)\b", "", words, flags=re.I)
    words = re.sub(r"(\s+v?\d+(\s+\d+)*)+\s*$", "", words.strip())  # trailing version numbers
    words = re.sub(r"\s+", " ", words).strip() or base
    return words[:1].upper() + words[1:] if words.islower() else words


# ---- environment and runners ---------------------------------------------------------------------

def host_env():
    """the environment for anything Hangar starts: without the AppImage's own libraries and paths"""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        # PyInstaller points LD_LIBRARY_PATH (and others) at the bundle and keeps the old value in KEY_ORIG
        if "LD_LIBRARY_PATH_ORIG" not in env:
            env.pop("LD_LIBRARY_PATH", None)
        for key in [k for k in env if k.endswith("_ORIG")]:
            value = env.pop(key)
            if value:
                env[key[:-5]] = value
            else:
                env.pop(key[:-5], None)
        for key in ("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
                    "SSL_CERT_FILE", "SSL_CERT_DIR", "GDK_PIXBUF_MODULE_FILE", "GIO_MODULE_DIR", "TCL_LIBRARY",
                    "TK_LIBRARY"):
            env.pop(key, None)
    for key in [k for k in env if k.startswith(("_PYI", "_MEI"))] + ["APPDIR", "APPIMAGE", "ARGV0", "OWD",
                                                                    "HANGAR_BSDTAR"]:
        env.pop(key, None)
    # Hangar's own tools (a downloaded winetricks) come last, after the system's
    env["PATH"] = env.get("PATH", "/usr/bin:/bin") + ":" + os.path.join(TOOLS, "bin")
    return env


def which(name):
    return shutil.which(name, path=host_env()["PATH"])


def natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def steam_root():
    for root in STEAM_ROOTS:
        if os.path.isdir(os.path.join(root, "steamapps")):
            return os.path.realpath(root)
    return None


def steam_libraries():
    libs = []
    for root in STEAM_ROOTS:
        try:
            with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        for p in re.findall(r'"path"\s*"([^"]+)"', text):
            p = os.path.realpath(p.replace("\\\\", "\\"))
            if p not in libs:
                libs.append(p)
    return libs


ARCHES = ("x86_64", "aarch64")
MACHINE = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine().lower(), platform.machine().lower())


def arch_of(name):
    """the CPU a Proton build/archive name says it's for ("" when it doesn't say)"""
    low = name.lower()
    return next((a for a in ARCHES if a in low), "")


def proton_label(name):
    for a in ARCHES:
        name = name.replace(f"-{a}", "")
    return name


def proton_install_dir():
    """where a downloaded GE-Proton goes: Steam's compatibilitytools.d, so Steam can use it too"""
    root = steam_root()
    return os.path.join(root, "compatibilitytools.d") if root else os.path.join(RUNNERS, "proton")


def proton_versions():
    """every Proton on this PC, newest GE-Proton first: [{name, path}]"""
    dirs = [os.path.join(r, "compatibilitytools.d") for r in STEAM_ROOTS]
    dirs += [os.path.join(lib, "steamapps", "common") for lib in steam_libraries()]
    dirs += [os.path.join(HOME, ".config/heroic/tools/proton"), os.path.join(RUNNERS, "proton"),
             "/usr/share/steam/compatibilitytools.d"]
    found = {}
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            real = os.path.realpath(p)
            if os.path.isfile(os.path.join(p, "proton")) and real not in found \
                    and arch_of(n) in ("", MACHINE):  # an ARM build on an x86 PC can't run (Exec format error)
                found[real] = {"name": proton_label(n), "path": p}
    vs = list(found.values())
    ge = sorted((v for v in vs if v["name"].lower().startswith("ge-proton")), key=lambda v: natural(v["name"]),
                reverse=True)
    rest = sorted((v for v in vs if not v["name"].lower().startswith("ge-proton")),
                  key=lambda v: natural(v["name"]), reverse=True)
    return ge + rest


_wine_label = {}


def wine_versions():
    """every Wine on this PC: the system's, then Lutris/Heroic/Bottles/Hangar runners: [{name, path}]"""
    out, seen = [], set()
    system = which("wine")
    if system:
        if system not in _wine_label:
            try:
                ver = subprocess.run([system, "--version"], capture_output=True, text=True, timeout=5,
                                     env=host_env()).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                ver = ""
            _wine_label[system] = f"System Wine ({ver})" if ver else "System Wine"
        out.append({"name": _wine_label[system], "path": system})
        seen.add(os.path.realpath(system))
    for d in (os.path.join(HOME, ".local/share/lutris/runners/wine"), os.path.join(HOME, ".config/heroic/tools/wine"),
              os.path.join(HOME, ".local/share/bottles/runners"),
              os.path.join(HOME, ".var/app/com.usebottles.bottles/data/bottles/runners"),
              os.path.join(RUNNERS, "wine")):
        try:
            names = sorted(os.listdir(d), key=natural, reverse=True)
        except OSError:
            continue
        for n in names:
            for b in ("wine", "wine64"):
                p = os.path.join(d, n, "bin", b)
                if os.path.isfile(p) and os.path.realpath(p) not in seen:
                    seen.add(os.path.realpath(p))
                    out.append({"name": n, "path": p})
                    break
    return out


def default_proton(s=None):
    s = s or settings()
    vs = proton_versions()
    if s["proton"] and any(v["path"] == s["proton"] for v in vs):
        return s["proton"]
    return vs[0]["path"] if vs else ""


def default_wine(s=None):
    s = s or settings()
    vs = wine_versions()
    if s["wine"] and any(v["path"] == s["wine"] for v in vs):
        return s["wine"]
    return vs[0]["path"] if vs else ""


def runner_path(e, s=None):
    """the Proton folder or wine binary this entry runs with ('' if none)"""
    s = s or settings()
    if e["runner"] == "proton":
        if e["version"] and os.path.isfile(os.path.join(e["version"], "proton")) \
                and arch_of(os.path.basename(e["version"])) in ("", MACHINE):
            return e["version"]
        return default_proton(s)
    if e["runner"] == "wine":
        if e["version"] and os.path.isfile(e["version"]):
            return e["version"]
        return default_wine(s)
    return ""


def runner_label(e, s=None):
    if e["runner"] == "native":
        return "Native"
    p = runner_path(e, s)
    if not p:
        return f"{RUNNER_NAMES[e['runner']]} (missing)"
    if e["runner"] == "proton":
        return proton_label(os.path.basename(p))
    match = next((v["name"] for v in wine_versions() if v["path"] == p), "Wine")
    return match


def proton_bin(proton, name):
    for sub in ("files", "dist"):
        p = os.path.join(proton, sub, "bin", name)
        if os.path.isfile(p):
            return p
    return ""


def prefix_of(e, s=None):
    s = s or settings()
    return os.path.expanduser(e["prefix"]) if e["prefix"] else os.path.join(os.path.expanduser(s["prefix_root"]),
                                                                           slug(e["name"]))


def wine_prefix(e, s=None):
    """the WINEPREFIX itself: <prefix>/pfx (Proton's layout), or the folder if it's a plain wine prefix"""
    p = prefix_of(e, s)
    return p if os.path.isdir(os.path.join(p, "drive_c")) and not os.path.isdir(os.path.join(p, "pfx")) \
        else os.path.join(p, "pfx")


def is_windows(path):
    return path.lower().endswith(WINDOWS_EXT)


def windows_cmd(target):
    low = target.lower()
    if low.endswith(".msi"):
        return ["msiexec", "/i", target]
    if low.endswith((".bat", ".cmd")):
        return ["cmd", "/c", target]
    if low.endswith(".lnk"):
        return ["start", "/unix", target]
    return [target]


class LaunchError(Exception):
    pass


ENV_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def parse_launch_options(text):
    """Steam-style launch options: ({env}, [command before the game], [arguments after it]).

    'VAR=1 OTHER=2 gamescope -f -- %command% -vr' puts VAR/OTHER in the environment, gamescope in front of
    the game and -vr after it. Without %command%, leading VAR=value words are the environment and the rest
    are arguments, so plain '-windowed -nointro' still works."""
    try:
        words = shlex.split(text or "")
    except ValueError as ex:
        raise LaunchError(f"The launch options don't parse: {ex}")
    env, pre, args = {}, [], []
    if "%command%" in words:
        i = words.index("%command%")
        before, args = words[:i], [w for w in words[i + 1:] if w != "%command%"]
        while before and ENV_TOKEN.match(before[0]):
            k, v = before.pop(0).split("=", 1)
            env[k] = v
        pre = before
    else:
        while words and ENV_TOKEN.match(words[0]):
            k, v = words.pop(0).split("=", 1)
            env[k] = v
        args = words
    return env, pre, args


XRIZER_DIRS = ["/opt/xrizer", "/usr/lib/xrizer", "/usr/share/xrizer", os.path.join(HOME, ".local/share/xrizer"),
               "/opt/opencomposite", os.path.join(HOME, ".local/share/OpenComposite")]
WIVRN_JSON = ["/usr/share/openxr/1/openxr_wivrn.json", "/usr/local/share/openxr/1/openxr_wivrn.json",
              os.path.join(HOME, ".local/share/openxr/1/openxr_wivrn.json")]


def config_home():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")


def xrizer_dir():
    return next((d for d in XRIZER_DIRS if os.path.isfile(os.path.join(d, "bin/linux64/vrclient.so"))), "")


def wivrn_running():
    try:
        return subprocess.run(["pgrep", "-x", "wivrn-server"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def start_wivrn():
    """starts the WiVRn server (its systemd user unit, or the binary); True if it was started"""
    if wivrn_running() or not which("wivrn-server"):
        return False
    env = host_env()
    try:
        has_unit = subprocess.run(["systemctl", "--user", "cat", "wivrn.service"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if has_unit and subprocess.run(["systemctl", "--user", "start", "wivrn.service"], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return True
        subprocess.Popen([which("wivrn-server")], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def vr_env(runner):
    """environment for VR games: WiVRn's OpenXR runtime and xrizer for OpenVR games, as a fallback when WiVRn
    hasn't set them as active (it does when a headset connects), and what Steam's container needs (umu)"""
    env = {}
    if not os.path.isfile(os.path.join(config_home(), "openxr/1/active_runtime.json")):
        wj = next((j for j in WIVRN_JSON if os.path.isfile(j)), "")
        if wj:
            env["XR_RUNTIME_JSON"] = wj
    if not os.path.isfile(os.path.join(config_home(), "openvr/openvrpaths.vrpath")):
        xr = xrizer_dir()
        if xr:
            env["VR_OVERRIDE"] = xr
    env["PRESSURE_VESSEL_IMPORT_OPENXR_1_RUNTIMES"] = "1"
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["PRESSURE_VESSEL_FILESYSTEMS_RW"] = os.path.join(runtime, "wivrn/comp_ipc")
    return env


VR_LIBS = {"openvr_api.dll", "openxr_loader.dll", "xrsdkopenvr.dll", "unityopenxr.dll", "libopenvr_api.so",
           "libopenxr_loader.so", "vrclient.dll", "openvr_api.so"}


# Steam's free anti-cheat runtimes: Proton's Wine loads the Linux side of EAC/BattlEye from the folder in
# PROTON_EAC_RUNTIME / PROTON_BATTLEYE_RUNTIME. key -> (label, env var, Steam app id, folder names)
ANTICHEATS = {
    "eac": ("EasyAntiCheat", "PROTON_EAC_RUNTIME", 1826330, ("Proton EasyAntiCheat Runtime", "eac_runtime")),
    "battleye": ("BattlEye", "PROTON_BATTLEYE_RUNTIME", 1161040, ("Proton BattlEye Runtime", "battleye_runtime")),
}


def anticheat_runtime(key):
    """the installed runtime folder for an ANTICHEATS key ('' if none): Steam's, then Heroic's"""
    names = ANTICHEATS[key][3]
    dirs = [os.path.join(lib, "steamapps", "common", names[0]) for lib in steam_libraries()]
    for heroic in (os.path.join(HOME, ".config/heroic"), os.path.join(HOME, ".var/app/com.heroicgameslauncher.hgl/config/heroic")):
        dirs.append(os.path.join(heroic, "tools", "runtimes", names[1]))
    return next((d for d in dirs if os.path.isdir(d) and os.listdir(d)), "")


def looks_anticheat(exe):
    """{"eac": bool, "battleye": bool}: what the program's folder ships"""
    out = {"eac": False, "battleye": False}
    folder = os.path.dirname(exe)
    if not os.path.isdir(folder) or folder in (HOME, "/"):
        return out
    base_depth = folder.count("/")
    seen = 0
    for dirpath, dirnames, filenames in os.walk(folder):
        if dirpath.count("/") - base_depth >= 3:
            dirnames[:] = []
        for n in dirnames + filenames:
            low = n.lower()
            seen += 1
            if low == "easyanticheat" or low.startswith(("easyanticheat_", "start_protected_game")):
                out["eac"] = True
            if low == "battleye" or low.startswith(("beservice", "beclient")):
                out["battleye"] = True
        if seen > 5000 or all(out.values()):
            break
    return out


def looks_vr(exe):
    """True when the program's folder ships OpenVR/OpenXR libraries"""
    folder = os.path.dirname(exe)
    if not os.path.isdir(folder) or folder in (HOME, "/"):
        return False
    base_depth = folder.count("/")
    seen = 0
    for dirpath, dirnames, filenames in os.walk(folder):
        if dirpath.count("/") - base_depth >= 4:
            dirnames[:] = []
        for fn in filenames:
            seen += 1
            if fn.lower() in VR_LIBS:
                return True
        if seen > 30000:
            break
    return False


def focused_output():
    """the name of the monitor that has focus (niri), or "" """
    if not os.environ.get("NIRI_SOCKET") or not which("niri"):
        return ""
    try:
        out = subprocess.run(["niri", "msg", "-j", "focused-output"], capture_output=True, text=True, timeout=2,
                             env=host_env())
        return (json.loads(out.stdout) or {}).get("name", "") if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def anticheat_env(e, env):
    """PROTON_EAC_RUNTIME / PROTON_BATTLEYE_RUNTIME for the anti-cheats the entry has turned on"""
    out = {}
    shared = []
    for key, (label, var, appid, _names) in ANTICHEATS.items():
        if not e.get(key):
            continue
        path = anticheat_runtime(key)
        if not path:
            raise LaunchError(f"{e['name']} needs the Proton {label} Runtime. It's free: install it from Steam "
                              f"(search \"Proton {label} Runtime\" with Tools shown in the library, or open "
                              f"steam://install/{appid}), or turn {label} off in the game's settings.")
        out[var] = path
        shared.append(path)
    if shared:  # umu's Steam Runtime container only sees home by default; a Steam library elsewhere must be shared
        old = env.get("PRESSURE_VESSEL_FILESYSTEMS_RO", "")
        out["PRESSURE_VESSEL_FILESYSTEMS_RO"] = ":".join([p for p in old.split(":") if p] + shared)
    return out


def build_command(e, s=None, target=None, extra=None, tool=False):
    """(command, environment, working folder) to run the entry, or `target` inside its prefix"""
    s = s or settings()
    env = host_env()
    exe = os.path.expanduser(target or e["exe"]).strip()
    if not exe:
        raise LaunchError("No program set. Edit it and pick what to run.")
    opt_env, opt_pre, opt_args = parse_launch_options(e["args"])
    if e["vr"]:
        vr_opt = parse_launch_options(s["vr_options"])
        opt_env = {**vr_opt[0], **opt_env}
        opt_pre, opt_args = vr_opt[1] + opt_pre, vr_opt[2] + opt_args
    args = extra if extra is not None else opt_args
    is_file = os.path.isfile(exe)
    cwd = os.path.expanduser(e["cwd"]) if e["cwd"] else (os.path.dirname(exe) if is_file else HOME)
    pre = []
    runner = e["runner"]
    if runner == "native":
        if is_file:
            if exe.endswith(".sh") and not os.access(exe, os.X_OK):
                cmd = ["sh", exe]
            else:
                if not os.access(exe, os.X_OK):
                    try:
                        os.chmod(exe, os.stat(exe).st_mode | 0o111)
                    except OSError:
                        raise LaunchError(f"{os.path.basename(exe)} isn't executable and can't be made so.")
                cmd = [exe]
        else:
            try:
                cmd = shlex.split(exe)
            except ValueError:
                cmd = []
            if not cmd or not (os.path.isfile(cmd[0]) or shutil.which(cmd[0], path=env["PATH"])):
                raise LaunchError(f"Can't find {exe}")
        cmd += args
        if e["mangohud"] and which("mangohud"):
            pre.append("mangohud")
    elif runner == "proton":
        proton = runner_path(e, s)
        if not proton:
            raise LaunchError("No Proton found. Get GE-Proton in Settings.")
        if target is None and not is_file:
            raise LaunchError(f"{exe} doesn't exist.")
        prefix = prefix_of(e, s)
        run = windows_cmd(exe) + args
        umu = which("umu-run") if s["use_umu"] else None
        if umu:
            env.update(WINEPREFIX=os.path.join(prefix, "pfx"), PROTONPATH=proton, GAMEID="umu-0", STORE="none")
            cmd = [umu] + run
        else:
            env.update(STEAM_COMPAT_DATA_PATH=prefix, STEAM_COMPAT_CLIENT_INSTALL_PATH=steam_root() or prefix,
                       STEAM_COMPAT_INSTALL_PATH=cwd if os.path.isdir(cwd) else prefix,
                       SteamAppId="0", SteamGameId="0", STEAM_COMPAT_APP_ID="0")
            cmd = [os.path.join(proton, "proton"), "run" if tool else "waitforexitandrun"] + run
        if e["mangohud"]:
            env["MANGOHUD"] = "1"
        env.update(anticheat_env(e, env))
        if s["wayland"] and not e["vr"] and os.environ.get("WAYLAND_DISPLAY"):
            # a native Wayland window: X11 windows get lost on monitors at negative positions (niri). Not for VR
            # games: Proton's OpenVR/DXVK bridge needs X11 (D3D11 fails to create its device otherwise)
            env["PROTON_ENABLE_WAYLAND"] = "1"
            # Wine takes the topmost monitor as the main one, so fullscreen games jumped to it: make the main
            # one the monitor you launch from (GE-Proton's Wayland driver reads this; the game's own env wins)
            focused = focused_output()
            if focused:
                env.setdefault("WAYLANDDRV_PRIMARY_MONITOR", focused)
    elif runner == "wine":
        wine = runner_path(e, s)
        if not wine:
            raise LaunchError("No Wine found. Install wine, or pick Proton instead.")
        if target is None and not is_file:
            raise LaunchError(f"{exe} doesn't exist.")
        env["WINEPREFIX"] = wine_prefix(e, s)
        env["PATH"] = os.path.dirname(wine) + ":" + env["PATH"]
        env.setdefault("WINEDEBUG", "-all")
        cmd = [wine] + windows_cmd(exe) + args
        if e["mangohud"]:
            env["MANGOHUD"] = "1"
        env.update(anticheat_env(e, env))  # only Proton-based Wine builds (GE/soda) read these
    else:
        raise LaunchError(f"Unknown runner {runner}")
    if e["gamemode"] and which("gamemoderun"):
        pre.insert(0, "gamemoderun")
    if extra is None:
        pre = opt_pre + pre
    if e["wrapper"].strip():
        try:
            pre = shlex.split(e["wrapper"]) + pre
        except ValueError as ex:
            raise LaunchError(f"The command prefix doesn't parse: {ex}")
    if e["vr"]:
        env.update(vr_env(runner))
    for line in e["env"].splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            if k.strip():
                env[k.strip()] = v.strip()
    env.update(opt_env)
    return pre + cmd, env, cwd if os.path.isdir(cwd) else HOME


def start_process(e, s=None, target=None, extra=None, tool=False, log_name=None):
    cmd, env, cwd = build_command(e, s, target, extra, tool)
    if e["runner"] == "proton":
        os.makedirs(prefix_of(e, s), exist_ok=True)
    elif e["runner"] == "wine":
        os.makedirs(env["WINEPREFIX"], exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    with open(os.path.join(LOGS, f"{log_name or e['id']}.log"), "w", encoding="utf-8") as log:
        log.write(f"# {time.ctime()}\n# {shlex.join(cmd)}\n# in {cwd}\n\n")
        log.flush()
        return subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)


def wine_tool_env(e, s=None):
    """(environment, wineserver) for wine tools (winetricks, wineserver -k) on the entry's prefix"""
    s = s or settings()
    env = host_env()
    path = runner_path(e, s)
    if e["runner"] == "proton":
        wine, server = proton_bin(path, "wine"), proton_bin(path, "wineserver")
        env["WINEPREFIX"] = os.path.join(prefix_of(e, s), "pfx")
    else:
        wine = path
        server = os.path.join(os.path.dirname(path), "wineserver")
        server = server if os.path.isfile(server) else (which("wineserver") or "")
        env["WINEPREFIX"] = wine_prefix(e, s)
    if wine:
        env["WINE"] = wine
        env["PATH"] = os.path.dirname(wine) + ":" + env["PATH"]
    if server:
        env["WINESERVER"] = server
    return env, server


def kill_prefix(e, s=None):
    if e["runner"] == "native":
        return
    env, server = wine_tool_env(e, s)
    if server and os.path.isdir(env["WINEPREFIX"]):
        try:
            subprocess.Popen([server, "-k"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            pass


def kill_group(p, sig):
    try:
        os.killpg(p.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def open_path(path):
    try:
        subprocess.Popen(["xdg-open", path], env=host_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


# ---- archives, folders and installers ----------------------------------------------------------

ARCHIVE_RE = re.compile(r"\.(zip|rar|7z|tar|tgz|txz|tbz2|tar\.(gz|xz|bz2|zst|lz4)|7z\.001)$", re.I)
LATER_PART_RE = re.compile(r"(\.part0*([2-9]|\d{2,})\.rar|\.r\d\d|\.7z\.(?!001)\d{3}|\.z\d\d)$", re.I)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
SKIP_PROGRAMS = re.compile(
    r"unins|uninstall|crash|redist|vc_?redist|dxsetup|dxwebsetup|directx|dotnet|ue4prereq|prereq|physx|"
    r"easyanticheat|battleye|notification_helper|qtwebengineprocess|cefsharp|subprocess|helper|reporter|"
    r"vcrun|oalinst|xnafx|7z(a|g|fm)?\.exe|unitycrashhandler|winrar|unrar|dosbox_?debug", re.I)
INSTALLER_RE = re.compile(r"^(setup|install|installer)([-_ .][^/]*)?\.exe$|[-_ ]setup[^/]*\.exe$|^setup_.*\.exe$", re.I)


def is_archive(path):
    return bool(ARCHIVE_RE.search(path)) and not LATER_PART_RE.search(path)


# an installer's own messages in its string table: two different ones are needed (a game's own strings can
# say "installer", French for "install": PlayRDR2.exe's "veuillez réinstaller le jeu")
INSTALLER_PHRASES = (r"\bInstalling\b", r"setup wizard", r"installation wizard", r"license agreement",
                     r"destination (folder|directory|location)", r"select (a|the) (location|folder) (to|where)",
                     r"\bready to install\b", r"\binstallation (is )?complete")
INSTALLER_STRINGS_RE = re.compile("|".join(f"({p})" for p in INSTALLER_PHRASES), re.I)


def installer_phrase_count(text):
    return len({i for m in INSTALLER_STRINGS_RE.finditer(text) for i, g in enumerate(m.groups()) if g})
INSTALLER_TEXT_RE = re.compile(r"\b(setup|installer|install|installation)\b", re.I)
# what installer builders leave in the file: NSIS's manifest name, Inno's data block, InstallShield's name
INSTALLER_MARKS = (b"Nullsoft.NSIS.exehead", b"Inno Setup Setup Data", b"InstallShield")
_installer_cache = {}


def pe_sections_and_version(path):
    """(section names, version-info text, string-table text) of a Windows .exe: its header, RT_VERSION and
    RT_STRING resources"""
    with open(path, "rb") as f:
        head = f.read(4096)
        if head[:2] != b"MZ":
            return [], "", ""
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        f.seek(pe)
        hdr = f.read(24 + 240 + 40 * 96)
        if hdr[:4] != b"PE\0\0":
            return [], "", ""
        nsec, = struct.unpack_from("<H", hdr, 6)
        opt_size, = struct.unpack_from("<H", hdr, 20)
        magic, = struct.unpack_from("<H", hdr, 24)
        dd = 24 + (96 if magic == 0x10B else 112)
        res_rva, _res_size = struct.unpack_from("<II", hdr, dd + 16)
        sections, names = [], []
        for i in range(min(nsec, 96)):
            o = 24 + opt_size + 40 * i
            if o + 40 > len(hdr):
                break
            names.append(hdr[o:o + 8].rstrip(b"\0").decode("latin-1"))
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", hdr, o + 8)
            sections.append((vaddr, max(vsize, rawsize), rawptr))
        text = strings = ""

        def at(off, n):
            f.seek(off)
            b = f.read(n)
            if len(b) != n:
                raise ValueError("short read")
            return b

        def off_of(rva):
            for va, size, raw in sections:
                if va <= rva < va + size:
                    return raw + rva - va
            raise ValueError("rva outside sections")

        if res_rva:
            base = off_of(res_rva)  # read on demand: installers keep their whole payload in .rsrc

            def entries(rel):
                named, ids = struct.unpack("<HH", at(base + rel + 12, 4))
                return [struct.unpack("<II", at(base + rel + 16 + 8 * i, 8)) for i in range(min(named + ids, 512))]

            def leaves(node, depth=0, out=None):
                out = [] if out is None else out
                if len(out) >= 64:
                    return out
                if node & 0x80000000:
                    if depth < 3:
                        for _n, sub in entries(node & 0x7FFFFFFF):
                            leaves(sub, depth + 1, out)
                else:
                    out.append(struct.unpack("<II", at(base + node, 8)))
                return out

            for name, off in entries(0):
                if name == 16 and off & 0x80000000:  # RT_VERSION: the first language's is enough
                    for rva, size in leaves(off)[:1]:
                        text = at(off_of(rva), min(size, 64 << 10)).decode("utf-16-le", "ignore")
                elif name == 6 and off & 0x80000000:  # RT_STRING: every language, capped
                    got = []
                    for rva, size in leaves(off):
                        got.append(at(off_of(rva), min(size, 64 << 10)).decode("utf-16-le", "ignore"))
                        if sum(map(len, got)) > 256 << 10:
                            break
                    strings = "\n".join(got)
        return names, text, strings


def installer_by_content(path):
    """True when the .exe says it's an installer: WiX Burn's section, version info calling it a setup or
    installer (Rockstar-Games-Launcher.exe: "Rockstar Games Launcher Installer"), or an installer builder's marks"""
    try:
        st = os.stat(path)
    except OSError:
        return False
    key = (path, st.st_size, st.st_mtime)
    if key in _installer_cache:
        return _installer_cache[key]
    found = False
    try:
        names, text, strings = pe_sections_and_version(path)
        if any(n.lower() == ".wixburn" for n in names):
            found = True
        else:
            # the values of FileDescription / ProductName / InternalName / OriginalFilename
            fields = re.findall(r"(?:FileDescription|ProductName|InternalName|OriginalFilename)\x00+([^\x00]+)", text)
            found = any(INSTALLER_TEXT_RE.search(v) for v in fields) or installer_phrase_count(strings) >= 2
        if not found:
            with open(path, "rb") as f:
                blob = f.read(2 << 20)
            found = any(m in blob for m in INSTALLER_MARKS)
    except (OSError, struct.error, ValueError, IndexError):
        found = False
    _installer_cache[key] = found
    return found


def is_installer(path):
    """an installer by its name (setup.exe, install_x.exe, x-setup.exe) or, for a real .exe file, by its contents"""
    if INSTALLER_RE.search(os.path.basename(path)):
        return True
    return path.lower().endswith(".exe") and os.path.isabs(path) and os.path.isfile(path) and installer_by_content(path)


def archive_name(path):
    """a game name from an archive's file name: without extensions, part numbers, [tags] and versions"""
    base = os.path.basename(path)
    base = re.sub(r"(\.part0*1)?\.(zip|rar|7z|tar|tgz|txz|tbz2|tar\.\w+|7z\.001)$", "", base, flags=re.I)
    base = re.sub(r"[\[({].*?[\])}]", " ", base)
    base = re.sub(r"[-_.]+", " ", base)
    base = re.sub(r"\b(v|ver|version|build)\s*\d[\d ]*\b.*$", "", base, flags=re.I)
    base = re.sub(r"\b(x64|x86|win64|win32|windows|linux|portable|repack|gog|steamrip|multi\d*)\b", "", base,
                  flags=re.I)
    base = re.sub(r"(\s+\d+)+\s*$", "", base.strip())
    base = re.sub(r"\s+", " ", base).strip() or os.path.basename(path)
    return base[:1].upper() + base[1:] if base.islower() else base


def unique_dir(parent, name):
    name = re.sub(r"[/\\:*?\"<>|]+", " ", name).strip() or "Game"
    path = os.path.join(parent, name)
    n = 2
    while os.path.exists(path):
        path = os.path.join(parent, f"{name} ({n})")
        n += 1
    return path


def bsdtar():
    return os.environ.get("HANGAR_BSDTAR") if os.path.isfile(os.environ.get("HANGAR_BSDTAR") or "") \
        else which("bsdtar")


def extract(archive, dest, report=lambda _x: None):
    """unpacks an archive into dest, with whichever tool can: unrar/7z/bsdtar/unzip/tar"""
    env = host_env()
    low = archive.lower()
    tools = []
    bt = bsdtar()
    if low.endswith(".7z.001"):
        tools = [["7z", "x", "-y", f"-o{dest}", archive]]
    elif low.endswith(".rar"):
        tools = [["unrar", "x", "-o+", "-y", "-idq", archive, dest + "/"], ["7z", "x", "-y", f"-o{dest}", archive]]
        if bt:
            tools.append([bt, "-xvf", archive, "-C", dest])
    else:
        if bt:
            tools.append([bt, "-xvf", archive, "-C", dest])
        tools.append(["7z", "x", "-y", f"-o{dest}", archive])
        if low.endswith(".zip"):
            tools.append(["unzip", "-o", "-q", archive, "-d", dest])
        else:
            tools.append(["tar", "-xf", archive, "-C", dest])
    errors = []
    for cmd in tools:
        exe = cmd[0] if os.path.isabs(cmd[0]) else shutil.which(cmd[0], path=env["PATH"])
        if not exe:
            continue
        os.makedirs(dest, exist_ok=True)
        total = 0
        if os.path.basename(exe) == "bsdtar":
            try:
                listing = subprocess.run([exe, "-tf", archive], env=env, capture_output=True, timeout=300)
                total = listing.stdout.count(b"\n")
            except (OSError, subprocess.SubprocessError):
                total = 0
        report(("unpack", 0, total))
        p = subprocess.Popen([exe] + cmd[1:], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        done, tail = 0, []
        for line in p.stdout:
            done += 1
            tail = (tail + [line.decode("utf-8", "replace").strip()])[-6:]
            if total and done % 20 == 0:
                report(("unpack", min(done, total), total))
        if p.wait() == 0:
            return dest
        errors.append(f"{os.path.basename(exe)}: " + (" / ".join(t for t in tail if t) or f"exit {p.returncode}"))
        # start the next tool on an empty folder
        for item in os.listdir(dest):
            path = os.path.join(dest, item)
            shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) and not os.path.islink(path) \
                else os.remove(path)
    if not errors:
        need = "7z" if low.endswith(".7z.001") else "bsdtar, unrar or 7z"
        raise RuntimeError(f"nothing here can unpack it; install {need}")
    raise RuntimeError(errors[-1] if len(errors) == 1 else "; ".join(errors))


def flatten(folder):
    """an archive with one folder inside: its contents move up into `folder`"""
    items = [i for i in os.listdir(folder) if i not in ("__MACOSX",)]
    if len(items) != 1 or not os.path.isdir(os.path.join(folder, items[0])):
        return
    tmp = os.path.join(folder, f".hangar-{uuid.uuid4().hex[:6]}")
    os.rename(os.path.join(folder, items[0]), tmp)
    for item in os.listdir(tmp):
        shutil.move(os.path.join(tmp, item), os.path.join(folder, item))
    os.rmdir(tmp)
    shutil.rmtree(os.path.join(folder, "__MACOSX"), ignore_errors=True)


def is_native_program(path):
    low = path.lower()
    if low.endswith((".x86_64", ".x86", ".appimage")):
        return True
    if low.endswith(".sh"):
        return os.path.basename(low) in ("start.sh", "run.sh", "launch.sh", "play.sh") or os.access(path, os.X_OK)
    if "." in os.path.basename(path) or not os.access(path, os.X_OK):
        return False
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def find_programs(folder, newer_than=0, max_depth=6):
    """likely main programs in a folder, best first: [path]"""
    want = re.sub(r"[^a-z0-9]", "", os.path.basename(folder.rstrip("/")).lower())
    found = []
    root_depth = folder.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(folder):
        depth = dirpath.count("/") - root_depth
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d.lower() not in ("windows", "__macosx", "_commonredist", "redist",
                                                                 "directx", "dotnetfx", "vcredist", "support",
                                                                 "temp", "appdata", "internet explorer",
                                                                 "windows nt", "windows media player",
                                                                 "common files", "msbuild")]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            win = fn.lower().endswith(".exe")
            if not (win or is_native_program(path)) or SKIP_PROGRAMS.search(fn):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if newer_than and st.st_ctime < newer_than:
                continue
            name = re.sub(r"[^a-z0-9]", "", os.path.splitext(fn)[0].lower())
            score = -depth * 12 + min(st.st_size, 300 << 20) / (6 << 20)
            if want and name and (name in want or want in name):
                score += 40
            if is_installer(fn):
                score -= 60
            if "launcher" in name:
                score -= 5
            found.append((score, path))
    return [p for _s, p in sorted(found, reverse=True)]


def copy_into(paths, dest, move=False, unpack=False, report=lambda _x: None):
    """copies (or moves) files and folders into dest, unpacking archives there if `unpack`"""
    os.makedirs(dest, exist_ok=True)
    for i, src in enumerate(paths):
        report(("copy", i, len(paths)))
        if unpack and os.path.isfile(src) and is_archive(src):
            extract(src, dest, report)
            continue
        target = os.path.join(dest, os.path.basename(src.rstrip("/")))
        if move:
            if os.path.isdir(target) and os.path.isdir(src):
                shutil.copytree(src, target, symlinks=True, dirs_exist_ok=True)
                shutil.rmtree(src)
            else:
                shutil.move(src, target)
        elif os.path.isdir(src):
            shutil.copytree(src, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(src, target)
    return dest


def game_folder(e):
    if e["cwd"] and os.path.isdir(os.path.expanduser(e["cwd"])):
        return os.path.expanduser(e["cwd"])
    exe = os.path.expanduser(e["exe"])
    return os.path.dirname(exe) if os.path.isfile(exe) else ""


# ---- menu shortcuts ----------------------------------------------------------------------------

# ---- opening programs from the file manager ---------------------------------------------------------
# Windows programs' MIME types: Hangar can be the app that opens them (double-click an .exe and it runs)
EXE_MIMES = ["application/x-ms-dos-executable", "application/x-msdownload", "application/x-msi",
             "application/x-ms-shortcut", "application/vnd.microsoft.portable-executable", "application/x-dosexec",
             "application/x-ms-ne-executable", "application/x-bat", "application/x-msdos-program"]
OPENER = os.path.join(MENU_DIR, "hangar-open.desktop")


def entry_for_path(path):
    """the library entry that runs this file, if there is one"""
    real = os.path.realpath(path)
    for e in library():
        exe = os.path.expanduser(e["exe"] or "")
        if exe and os.path.realpath(exe) == real:
            return e
    return None


def quick_entry(path, s=None):
    """an entry for running a file once: its library entry if it has one, else a new one (not saved)
    whose prefix is the one it would get in the library, so its saves are there if you add it later"""
    s = s or settings()
    e = entry_for_path(path)
    if e:
        return e, True
    e = dict(ENTRY_DEFAULTS)
    e.update(id="quick-" + hashlib.md5(os.path.realpath(path).encode()).hexdigest()[:10], name=guess_name(path),
             exe=path, runner=(s.get("windows_runner") or "proton") if is_windows(path) else "native",
             added=int(time.time()))
    return e, False


def register_opener(on):
    """Hangar opens Windows programs (or stops doing so): a hidden menu entry for their MIME types,
    made the default for them"""
    if on:
        os.makedirs(MENU_DIR, exist_ok=True)
        with open(OPENER, "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Hangar\nGenericName=Run with Hangar\n"
                    "Comment=Run this Windows program with Proton or Wine\n"
                    f"Exec={shlex.join(self_command() + ['--open'])} %f\n"
                    "Icon=hangar\nTerminal=false\nNoDisplay=true\nStartupWMClass=hangar\n"
                    f"MimeType={';'.join(EXE_MIMES)};\n")
        if which("xdg-mime"):
            subprocess.run(["xdg-mime", "default", os.path.basename(OPENER)] + EXE_MIMES, env=host_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.remove(OPENER)
        except OSError:
            pass
        # take it back out of mimeapps.list, so the type falls back to whatever else opens it
        mimeapps = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "mimeapps.list")
        try:
            with open(mimeapps, encoding="utf-8") as f:
                lines = f.readlines()
            keep = [ln for ln in lines if not (ln.split("=", 1)[0].strip() in EXE_MIMES and os.path.basename(OPENER) in ln)]
            if keep != lines:
                with open(mimeapps, "w", encoding="utf-8") as f:
                    f.writelines(keep)
        except OSError:
            pass
    if which("update-desktop-database"):
        subprocess.run(["update-desktop-database", MENU_DIR], env=host_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def restart_hangar():
    """starts a fresh Hangar (the updated AppImage) and quits this one"""
    if INSTANCE:  # let the new one become the running Hangar instead of handing over to this one
        INSTANCE.close()
    subprocess.Popen(self_command(), env=os.environ.copy(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    QApplication.quit()


def self_command():
    if APPIMAGE:
        return [APPIMAGE]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def desktop_dir():
    """the user's desktop folder (XDG_DESKTOP_DIR from user-dirs.dirs, else ~/Desktop)"""
    try:
        with open(os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"),
                               "user-dirs.dirs"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("XDG_DESKTOP_DIR="):
                    path = line.split("=", 1)[1].strip().strip('"').replace("$HOME", HOME)
                    if path and os.path.abspath(path) != HOME:
                        return path
    except OSError:
        pass
    return os.path.join(HOME, "Desktop")


def desktop_shortcut_path(e):
    """the desktop file for this entry; found by its X-Hangar-Id so a renamed entry keeps its file"""
    folder = desktop_dir()
    try:
        for fn in os.listdir(folder):
            if fn.endswith(".desktop"):
                try:
                    with open(os.path.join(folder, fn), encoding="utf-8", errors="replace") as f:
                        if f"\nX-Hangar-Id={e['id']}\n" in f.read(4096):
                            return os.path.join(folder, fn)
                except OSError:
                    pass
    except OSError:
        pass
    return None


def shortcut_path(e, where="menu"):
    if where == "desktop":
        return desktop_shortcut_path(e) or ""
    return os.path.join(MENU_DIR, f"hangar-{e['id']}.desktop")


def has_shortcut(e, where="menu"):
    path = shortcut_path(e, where)
    return bool(path) and os.path.exists(path)


def shortcut_text(e):
    icon = os.path.join(ICONS, e["icon"]) if e["icon"] else (os.path.join(COVERS, e["cover"]) if e["cover"] else "")
    name = e["name"].replace("\n", " ")
    return ("[Desktop Entry]\nType=Application\n"
            f"Name={name}\nComment=Run with Hangar\n"
            f"Exec={shlex.join(self_command() + ['--launch', e['id']])}\n"
            f"Icon={icon or 'applications-games'}\nTerminal=false\n"
            f"Categories={'Game;' if e['kind'] == 'game' else 'Utility;'}\n"
            f"X-Hangar-Id={e['id']}\n")


def make_shortcut(e, where="menu"):
    if where == "menu":
        os.makedirs(MENU_DIR, exist_ok=True)
        with open(shortcut_path(e), "w", encoding="utf-8") as f:
            f.write(shortcut_text(e))
        return
    folder = desktop_dir()
    os.makedirs(folder, exist_ok=True)
    stem = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "", e["name"].replace("\n", " ")).strip(" .") or "Game"
    old = desktop_shortcut_path(e)
    path = os.path.join(folder, stem + ".desktop")
    n = 2
    while os.path.exists(path) and path != old:
        path = os.path.join(folder, f"{stem} ({n}).desktop")
        n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(shortcut_text(e))
    os.chmod(path, 0o755)
    if old and old != path:
        try:
            os.remove(old)
        except OSError:
            pass
    if shutil.which("gio"):  # Nautilus/Nemo only run desktop files marked trusted
        subprocess.run(["gio", "set", path, "metadata::trusted", "true"], env=host_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def remove_shortcut(e, where="menu"):
    path = shortcut_path(e, where)
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def refresh_shortcuts(e):
    """rewrite whichever shortcuts this entry already has (after a rename, new icon, kind change)"""
    for where in ("menu", "desktop"):
        if has_shortcut(e, where):
            make_shortcut(e, where)


# ---- .exe icons --------------------------------------------------------------------------------

def exe_icon(path):
    """the biggest icon inside a Windows .exe, as PNG or ICO bytes (None if it has none)"""
    try:
        with open(path, "rb") as f:
            def at(off, n):
                f.seek(off)
                b = f.read(n)
                if len(b) != n:
                    raise ValueError("short read")
                return b
            if at(0, 2) != b"MZ":
                return None
            pe = struct.unpack("<I", at(0x3C, 4))[0]
            if at(pe, 4) != b"PE\0\0":
                return None
            nsec, = struct.unpack("<H", at(pe + 6, 2))
            opt_size, = struct.unpack("<H", at(pe + 20, 2))
            opt = pe + 24
            magic, = struct.unpack("<H", at(opt, 2))
            dd = opt + (96 if magic == 0x10B else 112)
            res_rva, res_size = struct.unpack("<II", at(dd + 16, 8))
            if not res_rva:
                return None
            sections = []
            for i in range(nsec):
                s = at(opt + opt_size + 40 * i, 40)
                vsize, vaddr, rawsize, rawptr = struct.unpack("<IIII", s[8:24])
                sections.append((vaddr, max(vsize, rawsize), rawptr))

            def off_of(rva):
                for vaddr, size, rawptr in sections:
                    if vaddr <= rva < vaddr + size:
                        return rawptr + rva - vaddr
                raise ValueError("rva outside sections")

            base = off_of(res_rva)

            def entries(rel):
                named, ids = struct.unpack("<HH", at(base + rel + 12, 4))
                out = []
                for i in range(min(named + ids, 4096)):
                    name, off = struct.unpack("<II", at(base + rel + 16 + 8 * i, 8))
                    out.append((name, off))
                return out

            def first_data(off):
                # walk down subdirectories to the first data entry
                for _ in range(4):
                    if off & 0x80000000:
                        sub = entries(off & 0x7FFFFFFF)
                        if not sub:
                            return None
                        off = sub[0][1]
                    else:
                        rva, size = struct.unpack("<II", at(base + off, 8))
                        if size > 32 * 1024 * 1024:
                            return None
                        return at(off_of(rva), size)
                return None

            types = dict(entries(0))
            if 14 not in types or 3 not in types or not (types[14] & 0x80000000) or not (types[3] & 0x80000000):
                return None
            icons = {}
            for name, off in entries(types[3] & 0x7FFFFFFF):
                if not name & 0x80000000:
                    icons[name] = off
            groups = entries(types[14] & 0x7FFFFFFF)
            if not groups:
                return None
            grp = first_data(groups[0][1])
            if not grp or len(grp) < 6:
                return None
            count, = struct.unpack("<H", grp[4:6])
            best = None
            for i in range(count):
                rec = grp[6 + 14 * i: 20 + 14 * i]
                if len(rec) < 14:
                    break
                w, h, colors, _r, planes, bits, size, iid = struct.unpack("<BBBBHHIH", rec)
                score = ((w or 256) * (h or 256), bits)
                if iid in icons and (best is None or score > best[0]):
                    best = (score, rec, iid)
            if not best:
                return None
            data = first_data(icons[best[2]])
            if not data:
                return None
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return data
            w, h, colors, _r, planes, bits, _size, _iid = struct.unpack("<BBBBHHIH", best[1])
            header = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", w, h, colors, 0, planes, bits,
                                                                len(data), 22)
            return header + data
    except (OSError, ValueError, struct.error):
        return None


def save_exe_icon(eid, exe):
    data = exe_icon(exe)
    if not data:
        return ""
    img = QImage()
    if not img.loadFromData(data) or img.isNull():
        return ""
    os.makedirs(ICONS, exist_ok=True)
    name = f"{eid}-{int(time.time())}.png"
    img.save(os.path.join(ICONS, name))
    return name


# ---- network -----------------------------------------------------------------------------------

def ssl_context():
    # the bundled OpenSSL looks for certificates where the build machine keeps them; certifi always works
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL = None


def http_get(url, headers=None, timeout=20):
    global SSL
    SSL = SSL or ssl_context()
    req = urllib.request.Request(url, headers={"User-Agent": f"Hangar/{VERSION}", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL) as r:
        return r.read()


def http_json(url, headers=None):
    return json.loads(http_get(url, headers).decode("utf-8"))


def cover_candidates(term, key=""):
    """possible covers for a name: [{label, url, thumb}] from SteamGridDB (with a key) and the Steam store"""
    out = []
    if key:
        h = {"Authorization": f"Bearer {key}"}
        try:
            games = http_json("https://www.steamgriddb.com/api/v2/search/autocomplete/" + urllib.parse.quote(term),
                              h).get("data", [])[:3]
            for g in games:
                grids = http_json(f"https://www.steamgriddb.com/api/v2/grids/game/{g['id']}"
                                  "?dimensions=600x900,342x482,660x930&types=static", h).get("data", [])[:8]
                out += [{"label": g["name"], "url": gr["url"], "thumb": gr.get("thumb") or gr["url"]} for gr in grids]
        except Exception as ex:
            print(f"hangar: SteamGridDB search failed: {ex}", file=sys.stderr)
    items = http_json("https://store.steampowered.com/api/storesearch/?" +
                      urllib.parse.urlencode({"term": term, "l": "english", "cc": "US"})).get("items", [])[:10]
    for it in items:
        url = f"https://shared.steamstatic.com/store_item_assets/steam/apps/{it['id']}/library_600x900_2x.jpg"
        out.append({"label": it["name"], "url": url, "thumb": url,
                    "fallback": f"https://shared.steamstatic.com/store_item_assets/steam/apps/{it['id']}/header.jpg"})
    return out


def save_cover(eid, data):
    img = QImage()
    if not img.loadFromData(data) or img.isNull():
        raise ValueError("not an image")
    if img.width() > 900:
        img = img.scaledToWidth(900, Qt.TransformationMode.SmoothTransformation)
    os.makedirs(COVERS, exist_ok=True)
    name = f"{eid}-{int(time.time() * 1000)}.png"
    img.save(os.path.join(COVERS, name))
    return name


def install_proton(source, report):
    """downloads the newest release of a PROTON_SOURCES Proton into compatibilitytools.d; returns its name"""
    _label, api, suffix = PROTON_SOURCES[source]
    rel = http_json(api)
    archives = [a for a in rel["assets"] if a["name"].endswith(suffix)]
    # releases can carry builds for several CPUs (GE-Proton11-12 lists aarch64 first): take this PC's,
    # or one that names no CPU (older releases were x86_64 only)
    asset = next((a for a in archives if arch_of(a["name"]) == MACHINE), None) or \
        next((a for a in archives if not arch_of(a["name"]) and MACHINE == "x86_64"), None)
    if not asset:
        raise RuntimeError(f"the release has no {suffix} for {MACHINE}")
    folder = asset["name"][:-len(suffix)]  # the archive's top folder: GE-Proton11-12-x86_64, dwproton-11.0-16-x86_64
    tag = proton_label(folder)  # the name proton_versions() shows
    dest = proton_install_dir()
    if os.path.isfile(os.path.join(dest, folder, "proton")):
        return tag
    sums = next((a for a in rel["assets"] if a["name"] == folder + ".sha512sum"), None) or \
        next((a for a in rel["assets"] if a["name"].endswith(".sha512sum") and not arch_of(a["name"])), None)
    expected = ""
    if sums:
        expected = http_get(sums["browser_download_url"]).decode().split()[0].lower()
    os.makedirs(dest, exist_ok=True)
    part = os.path.join(dest, f".{asset['name']}.part")
    digest = hashlib.sha512()
    req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": f"Hangar/{VERSION}"})
    global SSL
    SSL = SSL or ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or asset.get("size") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                report(("download", done, total, tag))
        if expected and digest.hexdigest() != expected:
            raise RuntimeError("the download is damaged (checksum mismatch); try again")
        report(("extract", 0, 0, tag))
        subprocess.run(["tar", "-xf", part, "-C", dest], env=host_env(), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    finally:
        try:
            os.remove(part)
        except OSError:
            pass
    return tag


def version_tuple(v):
    return tuple(int(n) for n in re.findall(r"\d+", v)[:3])


def check_update(_report=None):
    """the newest Hangar release on GitHub: {version, newer, url (the AppImage), page}"""
    rel = http_json(HANGAR_RELEASES)
    ver = rel["tag_name"].lstrip("v")
    asset = next((a for a in rel.get("assets", []) if a["name"].endswith(".AppImage")), None)
    return {"version": ver, "newer": version_tuple(ver) > version_tuple(VERSION),
            "url": asset["browser_download_url"] if asset else "", "size": asset["size"] if asset else 0,
            "page": rel.get("html_url") or HANGAR_PAGE}


def install_update(info, report):
    """downloads the release's AppImage over the running one (beside it first, then swapped in); returns the version"""
    if not APPIMAGE or not os.path.isfile(APPIMAGE):
        raise RuntimeError("Hangar isn't running from its AppImage")
    folder = os.path.dirname(APPIMAGE)
    if not os.access(folder, os.W_OK):
        raise RuntimeError(f"can't write to {folder}")
    part = os.path.join(folder, f".{os.path.basename(APPIMAGE)}.part")
    req = urllib.request.Request(info["url"], headers={"User-Agent": f"Hangar/{VERSION}"})
    global SSL
    SSL = SSL or ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or info["size"] or 0)
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                report(("download", done, total, info["version"]))
        with open(part, "rb") as f:
            if f.read(4) != b"\x7fELF":
                raise RuntimeError("the download isn't an AppImage")
        if total and os.path.getsize(part) != total:
            raise RuntimeError("the download is incomplete; try again")
        os.chmod(part, 0o755)
        os.replace(part, APPIMAGE)  # a new file, so the running copy keeps working until restart
    finally:
        try:
            os.remove(part)
        except OSError:
            pass
    return info["version"]


def install_winetricks(_report):
    data = http_get(WINETRICKS_URL, timeout=60)
    if not data.startswith(b"#!"):
        raise RuntimeError("unexpected download")
    os.makedirs(os.path.join(TOOLS, "bin"), exist_ok=True)
    path = os.path.join(TOOLS, "bin", "winetricks")
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, 0o755)
    return path


class Task(QThread):
    """runs fn(report) off the UI thread; report() sends progress to the UI"""
    progress = pyqtSignal(object)
    done = pyqtSignal(object, object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(self.progress.emit), None)
        except Exception as ex:
            self.done.emit(None, str(ex) or ex.__class__.__name__)


# ---- running -----------------------------------------------------------------------------------

class Launcher(QObject):
    """keeps track of what Hangar started: playtime, stopping, quick crashes"""
    changed = pyqtSignal()
    finished = pyqtSignal(str, int, float)

    def __init__(self):
        super().__init__()
        self.running = {}
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)

    def is_running(self, eid):
        return eid in self.running

    def launch(self, e):
        if e["id"] in self.running:
            return None
        p = start_process(e)
        update_entry(e["id"], last_played=int(time.time()))
        self.running[e["id"]] = (p, time.time())
        self.changed.emit()
        return p

    def stop(self, e):
        item = self.running.get(e["id"])
        if not item:
            return
        p = item[0]
        kill_group(p, signal.SIGTERM)
        kill_prefix(e)
        QTimer.singleShot(5000, lambda: p.poll() is None and kill_group(p, signal.SIGKILL))

    def poll(self):
        ended = False
        for eid, (p, start) in list(self.running.items()):
            code = p.poll()
            if code is not None:
                del self.running[eid]
                secs = time.time() - start
                add_playtime(eid, secs)
                self.finished.emit(eid, code, secs)
                ended = True
        if ended:
            self.changed.emit()

    def save_running(self):
        """counts the time so far of everything still running (when Hangar closes)"""
        for eid, (_p, start) in self.running.items():
            add_playtime(eid, time.time() - start)
        self.running.clear()


# ---- themes ------------------------------------------------------------------------------------

def theme_dict(bg, surface, surface2, border, text, muted, accent):
    return dict(bg=bg, surface=surface, surface2=surface2, border=border, text=text, muted=muted, accent=accent)


THEMES = {
    "Midnight": theme_dict("#101014", "#18181e", "#22222a", "#2c2c36", "#e8e8ef", "#8a8a99", "#7c8cff"),
    "Gold": theme_dict("#121216", "#1b1b21", "#24242c", "#30303a", "#e8e8ef", "#8a8a99", "#c9a96e"),
    "Tokyo Night": theme_dict("#16161e", "#1a1b26", "#24283b", "#2f3549", "#c0caf5", "#7f88b3", "#7aa2f7"),
    "Catppuccin": theme_dict("#181825", "#1e1e2e", "#313244", "#45475a", "#cdd6f4", "#9399b2", "#cba6f7"),
    "Dracula": theme_dict("#1e1f29", "#282a36", "#343746", "#44475a", "#f8f8f2", "#9ea1b8", "#bd93f9"),
    "Rosé Pine": theme_dict("#191724", "#1f1d2e", "#26233a", "#403d52", "#e0def4", "#908caa", "#ebbcba"),
    "Nord": theme_dict("#2e3440", "#3b4252", "#434c5e", "#4c566a", "#eceff4", "#a7b0c0", "#88c0d0"),
    "Gruvbox": theme_dict("#1d2021", "#282828", "#32302f", "#45403d", "#ebdbb2", "#a89984", "#fabd2f"),
    "Emerald": theme_dict("#0e1311", "#151c19", "#1d2622", "#29342f", "#e4efe9", "#86998f", "#4ade80"),
    "Crimson": theme_dict("#141012", "#1d1619", "#271d21", "#36282e", "#f0e6e9", "#9c8a90", "#f43f5e"),
    "OLED": theme_dict("#000000", "#0b0b0d", "#151518", "#24242a", "#ededf2", "#85858f", "#e8e8ef"),
    "Light": theme_dict("#f3f3f6", "#ffffff", "#ececf1", "#d9d9e1", "#1d1d24", "#6b6b78", "#4f5bd5"),
}


def current_theme(s=None):
    s = s or settings()
    t = dict(THEMES.get(s["theme"], THEMES["Midnight"]))
    if s["accent"] and QColor(s["accent"]).isValid():
        t["accent"] = s["accent"]
    acc = QColor(t["accent"])
    t["on_accent"] = "#111116" if acc.lightnessF() > 0.55 else "#ffffff"
    t["accent_hover"] = acc.lighter(112).name() if acc.lightnessF() < 0.8 else acc.darker(108).name()
    t["light"] = QColor(t["bg"]).lightnessF() > 0.5
    return t


def rgba(hex_color, alpha):
    c = QColor(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


def stylesheet(t):
    return f"""
QMainWindow, QDialog, #root {{ background: {t['bg']}; }}
QLabel {{ color: {t['text']}; }}
QLabel#muted {{ color: {t['muted']}; }}
QLabel#title {{ font-size: 22px; font-weight: 700; }}
QLineEdit#title {{ font-size: 18px; font-weight: 700; background: transparent; border: none; padding: 0; }}
QLabel#h2 {{ font-size: 15px; font-weight: 700; }}
QLabel#name {{ font-weight: 600; }}
QLabel#badge {{ background: {t['surface2']}; color: {t['muted']}; border-radius: 7px; padding: 2px 8px; font-size: 11px; }}
QLabel#badgeAccent {{ background: {rgba(t['accent'], 0.16)}; color: {t['accent']}; border-radius: 7px; padding: 2px 8px; font-size: 11px; }}
QLabel#badgeNsfw {{ background: rgba(224,108,180,0.16); color: #ee93c8; border-radius: 7px; padding: 2px 8px; font-size: 11px; }}
QLabel#badgeRun {{ background: rgba(120,200,140,0.16); color: #79d494; border-radius: 7px; padding: 2px 8px; font-size: 11px; }}
QPushButton {{ background: {t['surface2']}; color: {t['text']}; border: 1px solid {t['border']}; border-radius: 9px; padding: 7px 14px; }}
QPushButton:hover {{ background: {rgba(t['accent'], 0.10)}; border: 1px solid {rgba(t['accent'], 0.35)}; }}
QPushButton:disabled {{ color: {t['muted']}; }}
QPushButton#primary {{ background: {t['accent']}; color: {t['on_accent']}; border: none; font-weight: 700; }}
QPushButton#primary:hover {{ background: {t['accent_hover']}; }}
QPushButton#stop {{ background: #e5534b; color: #ffffff; border: none; font-weight: 700; }}
QPushButton#stop:hover {{ background: #ee6a62; }}
QPushButton#round {{ background: {t['accent']}; color: {t['on_accent']}; border: none; border-radius: 19px; font-size: 15px; padding: 0; }}
QPushButton#round:hover {{ background: {t['accent_hover']}; }}
QPushButton#roundStop {{ background: #e5534b; color: #ffffff; border: none; border-radius: 19px; font-size: 13px; padding: 0; }}
QPushButton#seg {{ border-radius: 8px; padding: 6px 14px; background: transparent; border: 1px solid transparent; color: {t['muted']}; }}
QPushButton#seg:hover {{ color: {t['text']}; }}
QPushButton#seg:checked {{ background: {t['surface2']}; color: {t['text']}; border: 1px solid {t['border']}; }}
QPushButton#cover {{ background: transparent; border: 2px solid transparent; border-radius: 12px; padding: 0; }}
QPushButton#cover:hover {{ border: 2px solid {t['accent']}; }}
QToolButton {{ background: transparent; color: {t['muted']}; border: none; border-radius: 8px; padding: 4px 8px; font-size: 16px; }}
QToolButton:hover {{ background: {t['surface2']}; color: {t['text']}; }}
QToolButton::menu-indicator {{ image: none; }}
QLineEdit, QPlainTextEdit {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border']}; border-radius: 10px; padding: 7px 11px; selection-background-color: {t['accent']}; selection-color: {t['on_accent']}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {t['accent']}; }}
QComboBox {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border']}; border-radius: 10px; padding: 6px 11px; }}
QComboBox:focus {{ border: 1px solid {t['accent']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border']}; selection-background-color: {t['surface2']}; selection-color: {t['text']}; outline: none; padding: 4px; }}
QCheckBox {{ color: {t['text']}; spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {t['border']}; background: {t['surface']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border: 1px solid {t['accent']}; }}
QCheckBox:disabled {{ color: {t['muted']}; }}
QFrame#card {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 14px; }}
QFrame#card:hover {{ border: 1px solid {rgba(t['accent'], 0.45)}; }}
QFrame#tile {{ background: transparent; border: none; }}
QFrame#panel {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 14px; }}
QFrame#segbox {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QFrame#drop {{ background: {rgba(t['accent'], 0.10)}; border: 2px dashed {t['accent']}; border-radius: 18px; }}
QProgressBar {{ background: {t['surface2']}; border: none; border-radius: 5px; height: 10px; color: transparent; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 5px; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['border']}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QMenu {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border']}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t['surface2']}; }}
QMenu::item:disabled {{ color: {t['muted']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 5px 8px; }}
QToolTip {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border']}; padding: 5px; }}
"""


def apply_theme(app, s=None):
    t = current_theme(s)
    app.setStyle("Fusion")
    p = QPalette()
    for role, c in ((QPalette.ColorRole.Window, t["bg"]), (QPalette.ColorRole.WindowText, t["text"]),
                    (QPalette.ColorRole.Base, t["surface"]), (QPalette.ColorRole.AlternateBase, t["surface2"]),
                    (QPalette.ColorRole.Text, t["text"]), (QPalette.ColorRole.Button, t["surface2"]),
                    (QPalette.ColorRole.ButtonText, t["text"]), (QPalette.ColorRole.Highlight, t["accent"]),
                    (QPalette.ColorRole.HighlightedText, t["on_accent"]), (QPalette.ColorRole.ToolTipBase, t["surface"]),
                    (QPalette.ColorRole.ToolTipText, t["text"]), (QPalette.ColorRole.PlaceholderText, t["muted"])):
        p.setColor(role, QColor(c))
    app.setPalette(p)
    app.setStyleSheet(stylesheet(t))
    PIX_CACHE.clear()
    return t


# ---- pictures ----------------------------------------------------------------------------------

PIX_CACHE = {}


def rounded(pix, w, h, r=10, fit=False, bg=None):
    """pix cropped (or fitted) to w x h with rounded corners"""
    out = QPixmap(w, h)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), r, r)
    p.setClipPath(path)
    if bg:
        p.fillPath(path, bg)
    mode = Qt.AspectRatioMode.KeepAspectRatio if fit else Qt.AspectRatioMode.KeepAspectRatioByExpanding
    scaled = pix.scaled(w, h, mode, Qt.TransformationMode.SmoothTransformation)
    p.drawPixmap((w - scaled.width()) // 2, (h - scaled.height()) // 2, scaled)
    p.end()
    return out


def gradient_for(name, w, h):
    hue = sum(map(ord, name)) % 360
    grad = QLinearGradient(0, 0, w, h)
    grad.setColorAt(0, QColor.fromHsl(hue, 120, 92))
    grad.setColorAt(1, QColor.fromHsl((hue + 45) % 360, 130, 40))
    return grad


def placeholder(name, w, h, icon=None, r=10):
    """a gradient with the entry's icon, or its initials"""
    out = QPixmap(w, h)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), r, r)
    p.fillPath(path, gradient_for(name, w, h))
    if icon is not None and not icon.isNull():
        side = int(min(w, h) * (0.55 if h > w * 1.2 else 0.7))
        ic = icon.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        y = (h - ic.height()) // 2 - (int(h * 0.06) if h > w * 1.2 else 0)
        p.drawPixmap((w - ic.width()) // 2, y, ic)
        if h > w * 1.2:
            f = QFont()
            f.setBold(True)
            f.setPixelSize(max(11, int(w * 0.085)))
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 235))
            text = QFontMetrics(f).elidedText(name, Qt.TextElideMode.ElideRight, w - 20)
            p.drawText(QRectF(10, y + ic.height() + 8, w - 20, h * 0.2), Qt.AlignmentFlag.AlignHCenter, text)
    else:
        initials = "".join(x[0] for x in re.findall(r"[A-Za-z0-9]+", name)[:2]).upper() or "?"
        f = QFont()
        f.setBold(True)
        f.setPixelSize(int(min(w, h) * 0.34))
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 225))
        p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, initials)
    p.end()
    return out


def blurred(pix):
    w, h = pix.width(), pix.height()
    small = pix.scaled(max(1, w // 14), max(1, h // 14), Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    big = small.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(w, h)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), 10, 10)
    p.setClipPath(path)
    p.drawPixmap(0, 0, big)
    p.fillRect(0, 0, w, h, QColor(0, 0, 0, 70))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(max(11, min(w, h) // 7))
    p.setFont(f)
    p.setPen(QColor(255, 255, 255, 230))
    p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "NSFW")
    p.end()
    return out


def picture(e, w, h, shape, blur=False, r=10):
    """the entry's picture: shape 'cover' (portrait) or 'icon' (square)"""
    cover = os.path.join(COVERS, e["cover"]) if e["cover"] else ""
    icon = os.path.join(ICONS, e["icon"]) if e["icon"] else ""
    key = (e["id"], e["name"], cover, icon, w, h, shape, blur, r)
    if key in PIX_CACHE:
        return PIX_CACHE[key]
    icon_pix = QPixmap(icon) if icon else QPixmap()
    cover_pix = QPixmap(cover) if cover else QPixmap()
    if shape == "icon" and not icon_pix.isNull():
        out = rounded(icon_pix, w, h, r, fit=True)
    elif not cover_pix.isNull():
        out = rounded(cover_pix, w, h, r)
    else:
        out = placeholder(e["name"], w, h, icon_pix if not icon_pix.isNull() else None, r)
    if blur:
        out = blurred(out)
    PIX_CACHE[key] = out
    return out


def human_time(secs):
    secs = int(secs)
    if secs < 60:
        return "under a minute" if secs > 0 else "never"
    h, m = divmod(secs // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def human_when(ts):
    if not ts:
        return "never"
    days = (time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)) - time.mktime(
        time.localtime(ts)[:3] + (0, 0, 0, 0, 0, -1))) // 86400
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{int(days)} days ago"
    return time.strftime("%d %b %Y", time.localtime(ts))


# ---- small widgets -----------------------------------------------------------------------------

def label(text, name=None, wrap=False):
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


def button(text, slot=None, name=None, tip=None):
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if name:
        b.setObjectName(name)
    if tip:
        b.setToolTip(tip)
    if slot:
        b.clicked.connect(lambda _=False: slot())
    return b


def segments(options, current, on_pick):
    """a row of exclusive buttons in a box; returns (box, {key: button})"""
    box = QFrame()
    box.setObjectName("segbox")
    lay = QHBoxLayout(box)
    lay.setContentsMargins(3, 3, 3, 3)
    lay.setSpacing(2)
    group = QButtonGroup(box)
    buttons = {}
    for key, text in options.items():
        b = QPushButton(text)
        b.setObjectName("seg")
        b.setCheckable(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False, k=key: on_pick(k))
        group.addButton(b)
        lay.addWidget(b)
        buttons[key] = b
    if current in buttons:
        buttons[current].setChecked(True)
    return box, buttons


class Elided(QLabel):
    """a one-line label that shortens its text with … to fit"""

    def __init__(self, text, name=None):
        super().__init__()
        self.full = text
        if name:
            self.setObjectName(name)
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.setText(self.fontMetrics().elidedText(self.full, Qt.TextElideMode.ElideRight, self.width()))


# ---- dialogs -----------------------------------------------------------------------------------

class CoverSearch(QDialog):
    """search Steam (and SteamGridDB) for covers; pick one"""

    def __init__(self, parent, term):
        super().__init__(parent)
        self.setWindowTitle("Find a cover")
        self.resize(820, 600)
        self.data = None
        self.task = None
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(12)
        row = QHBoxLayout()
        self.term = QLineEdit(term)
        self.term.setPlaceholderText("Game name")
        self.term.returnPressed.connect(self.search)
        row.addWidget(self.term, 1)
        row.addWidget(button("Search", self.search, "primary"))
        v.addLayout(row)
        key = settings()["sgdb_key"]
        self.note = label("Covers from the Steam store" + (" and SteamGridDB" if key else
                          ". Add a SteamGridDB key in Settings for games that aren't on Steam."), "muted", wrap=True)
        v.addWidget(self.note)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.holder = QWidget()
        self.holder.setObjectName("holder")
        self.holder.setStyleSheet("#holder { background: transparent; }")
        self.grid = QGridLayout(self.holder)
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self.holder)
        v.addWidget(self.scroll, 1)
        self.count = 0
        if term.strip():
            QTimer.singleShot(0, self.search)

    def search(self):
        term = self.term.text().strip()
        if not term:
            return
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.count = 0
        self.note.setText("Searching…")
        key = settings()["sgdb_key"]

        def work(report):
            found = 0
            for c in cover_candidates(term, key):
                for url in (c["thumb"], c.get("fallback")):
                    if not url:
                        continue
                    try:
                        data = http_get(url, timeout=15)
                    except Exception:
                        continue
                    c["got"] = url
                    report((c, data))
                    found += 1
                    break
            return found

        if self.task and self.task.isRunning():
            self.task.progress.disconnect()
            self.task.done.disconnect()
        self.task = Task(work, self)
        self.task.progress.connect(self.add_result)
        self.task.done.connect(self.searched)
        self.task.start()

    def add_result(self, item):
        c, data = item
        pix = QPixmap()
        if not pix.loadFromData(data):
            return
        b = QPushButton()
        b.setObjectName("cover")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFixedSize(144, 214)
        b.setIcon(QIcon(rounded(pix, 136, 206, 10)))
        b.setIconSize(QSize(136, 206))
        b.setToolTip(c["label"])
        b.clicked.connect(lambda _=False: self.pick(c, data))
        cols = max(1, (self.scroll.viewport().width() - 10) // 156)
        self.grid.addWidget(b, self.count // cols, self.count % cols)
        self.count += 1

    def searched(self, found, error):
        if error:
            self.note.setText(f"Search failed: {error}")
        elif not found:
            self.note.setText("Nothing found. Try a shorter or different name, or choose an image file.")
        else:
            self.note.setText("Click a cover to use it.")

    def pick(self, c, data):
        if c["got"] != c["url"]:
            try:
                data = http_get(c["url"], timeout=30)
            except Exception:
                pass
        self.data = data
        if self.exe_opener.isChecked() != self.s["exe_opener"]:
            register_opener(self.exe_opener.isChecked())
        self.accept()

    def done(self, r):
        if self.task and self.task.isRunning():
            self.task.progress.disconnect()
            self.task.done.disconnect()
            self.task.setParent(None)
            RUNNING_TASKS.append(self.task)
            self.task.finished.connect(lambda t=self.task: RUNNING_TASKS.remove(t) if t in RUNNING_TASKS else None)
        super().done(r)


RUNNING_TASKS = []


class EditDialog(QDialog):
    """add or edit a game or app"""

    def __init__(self, parent, entry=None, kind="game", exe="", preset=None, start_dir=""):
        super().__init__(parent)
        self.s = settings()
        self.new = entry is None
        self.start_dir = start_dir
        self.e = dict(ENTRY_DEFAULTS, **(entry or {}))
        if self.new:
            self.e.update(id=new_id(), kind=kind, added=int(time.time()))
            if exe:
                self.e.update(exe=exe, name=guess_name(exe),
                              runner=self.s["windows_runner"] if is_windows(exe) else "native")
            self.e.update(preset or {})
            if self.e["exe"] and os.path.isfile(self.e["exe"]) and "vr" not in (preset or {}):
                self.e["vr"] = looks_vr(self.e["exe"])
            if self.e["exe"] and os.path.isfile(self.e["exe"]) and is_windows(self.e["exe"]) \
                    and "eac" not in (preset or {}):
                self.e.update(looks_anticheat(self.e["exe"]))
        self.cover_data = None
        self.cover_cleared = False
        self.icon_src = None
        self.icon_cleared = False
        self.setWindowTitle(("Add a game" if self.e["kind"] == "game" else "Add an app") if self.new
                            else f"Edit {self.e['name']}")
        self.resize(760, 660)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)
        body = QHBoxLayout()
        body.setSpacing(18)
        outer.addLayout(body, 1)

        # left: the cover
        left = QVBoxLayout()
        left.setSpacing(8)
        self.cover = QLabel()
        self.cover.setFixedSize(180, 270)
        left.addWidget(self.cover)
        left.addWidget(button("Find online…", self.find_cover, tip="Search the Steam store / SteamGridDB"))
        left.addWidget(button("Choose image…", self.choose_cover))
        left.addWidget(button("No cover", self.clear_cover))
        left.addSpacing(10)
        iconrow = QHBoxLayout()
        self.icon = QLabel()
        self.icon.setFixedSize(40, 40)
        iconrow.addWidget(self.icon)
        ib = QToolButton()
        ib.setText("Icon ▾")
        ib.setStyleSheet("font-size: 13px;")
        ib.setCursor(Qt.CursorShape.PointingHandCursor)
        ib.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        im = QMenu(ib)
        im.addAction("Take it from the .exe", self.icon_from_exe)
        im.addAction("Choose image…", self.choose_icon)
        im.addAction("No icon", self.clear_icon)
        ib.setMenu(im)
        iconrow.addWidget(ib)
        iconrow.addStretch(1)
        left.addLayout(iconrow)
        left.addSpacing(10)
        sl = QLabel("Make shortcut")
        sl.setObjectName("muted")
        left.addWidget(sl)
        self.desktop_btn = button("", self.toggle_desktop,
                                  tip="A launcher for this on your desktop; it's made or removed when you save")
        self.desktop_btn.setCheckable(True)
        self.desktop_btn.setChecked(not self.new and has_shortcut(self.e, "desktop"))
        self.toggle_desktop()
        left.addWidget(self.desktop_btn)
        left.addStretch(1)
        body.addLayout(left)

        # right: the details
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        formw = QWidget()
        formw.setObjectName("formw")
        formw.setStyleSheet("#formw { background: transparent; }")
        scroll.viewport().setObjectName("vp")
        scroll.viewport().setStyleSheet("#vp { background: transparent; }")
        form = QFormLayout(formw)
        form.setContentsMargins(0, 0, 8, 0)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        scroll.setWidget(formw)
        body.addWidget(scroll, 1)

        self.name = QLineEdit(self.e["name"])
        self.name.setPlaceholderText("Name")
        self.name.textChanged.connect(self.update_previews)
        self.name.textChanged.connect(self.update_prefix_hint)
        form.addRow("Name", self.name)

        kbox, self.kind_buttons = segments(KINDS, self.e["kind"], lambda k: None)
        krow = QHBoxLayout()
        krow.addWidget(kbox)
        krow.addStretch(1)
        form.addRow("Shows in", krow)

        exerow = QHBoxLayout()
        self.exe = QLineEdit(self.e["exe"])
        self.exe.setPlaceholderText("Program file, or a command (flatpak run …)")
        self.exe.editingFinished.connect(self.exe_changed)
        exerow.addWidget(self.exe, 1)
        exerow.addWidget(button("Browse…", self.browse_exe))
        form.addRow("Runs", exerow)

        self.args = QLineEdit(self.e["args"])
        self.args.setPlaceholderText("Optional, like Steam: -windowed, or VAR=1 %command% -vr")
        self.args.setToolTip("Works like Steam's launch options. Words after the game are arguments. With "
                             "%command%, VAR=value words before it go into the environment and any other words "
                             "run in front of the game (e.g. gamescope -f --).")
        form.addRow("Launch options", self.args)

        rbox, self.runner_buttons = segments(RUNNER_NAMES, self.e["runner"], self.set_runner)
        rrow = QHBoxLayout()
        rrow.addWidget(rbox)
        rrow.addStretch(1)
        form.addRow("Run with", rrow)

        self.version = QComboBox()
        self.version_label = QLabel("Version")
        form.addRow(self.version_label, self.version)

        prow = QHBoxLayout()
        self.prefix = QLineEdit(self.e["prefix"])
        prow.addWidget(self.prefix, 1)
        prow.addWidget(button("Browse…", self.browse_prefix))
        self.prefix_label = QLabel("Prefix")
        self.prefix_box = QWidget()
        self.prefix_box.setLayout(prow)
        prow.setContentsMargins(0, 0, 0, 0)
        form.addRow(self.prefix_label, self.prefix_box)

        crow = QHBoxLayout()
        self.cwd = QLineEdit(self.e["cwd"])
        self.cwd.setPlaceholderText("Optional; the program's own folder by default")
        crow.addWidget(self.cwd, 1)
        crow.addWidget(button("Browse…", self.browse_cwd))
        form.addRow("Start in", crow)

        self.wrapper = QLineEdit(self.e["wrapper"])
        self.wrapper.setPlaceholderText("Optional, e.g. gamescope -f -W 2560 -H 1440 --")
        form.addRow("Command prefix", self.wrapper)

        self.env = QPlainTextEdit(self.e["env"])
        self.env.setPlaceholderText("Optional, one per line, e.g.\nDXVK_HUD=fps\nPROTON_ENABLE_WAYLAND=1")
        self.env.setFixedHeight(84)
        form.addRow("Environment", self.env)

        opts = QGridLayout()
        opts.setHorizontalSpacing(18)
        self.mangohud = QCheckBox("MangoHud")
        self.mangohud.setChecked(self.e["mangohud"])
        self.gamemode = QCheckBox("GameMode")
        self.gamemode.setChecked(self.e["gamemode"])
        if not which("gamemoderun"):
            self.gamemode.setEnabled(self.e["gamemode"])
            self.gamemode.setToolTip("gamemoderun isn't installed")
        if not which("mangohud"):
            self.mangohud.setToolTip("mangohud isn't installed")
        self.favorite = QCheckBox("Favourite")
        self.favorite.setChecked(self.e["favorite"])
        self.nsfw = QCheckBox("NSFW")
        self.nsfw.setChecked(self.e["nsfw"])
        self.vr = QCheckBox("VR game")
        self.vr.setChecked(self.e["vr"])
        self.vr.setToolTip("Sets up WiVRn (OpenXR) and xrizer (OpenVR) for it, starts the WiVRn server if it isn't "
                           "running, and adds the VR launch options from Settings")
        self.hidden = QCheckBox("Hidden")
        self.hidden.setChecked(self.e["hidden"])
        self.eac = QCheckBox("EasyAntiCheat")
        self.battleye = QCheckBox("BattlEye")
        for key, cb in (("eac", self.eac), ("battleye", self.battleye)):
            label_, _var, appid, _names = ANTICHEATS[key]
            cb.setChecked(self.e[key])
            have = anticheat_runtime(key)
            cb.setToolTip(f"Loads Steam's Proton {label_} Runtime, for games whose {label_} supports Linux "
                          "(Proton/Wine only). " + (f"Found: {have}" if have else
                                                    f"Not installed yet: get it free from Steam (steam://install/{appid})"))
        for i, cb in enumerate((self.vr, self.mangohud, self.gamemode, self.favorite, self.nsfw, self.hidden,
                                self.eac, self.battleye)):
            opts.addWidget(cb, i // 3, i % 3)
        form.addRow("Options", opts)

        buttons = QDialogButtonBox()
        ok = buttons.addButton("Add" if self.new else "Save", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setObjectName("primary")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole).setCursor(Qt.CursorShape.PointingHandCursor)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self.set_runner(self.e["runner"], initial=True)
        self.update_previews()

    def toggle_desktop(self):
        # the button is checkable, so a click has already flipped it; only the label changes here
        self.desktop_btn.setText("On the desktop ✓" if self.desktop_btn.isChecked() else "Add to desktop")

    # runner
    def set_runner(self, runner, initial=False):
        self.runner = runner
        self.runner_buttons[runner].setChecked(True)
        windows = runner != "native"
        for w in (self.version, self.version_label, self.prefix_box, self.prefix_label):
            w.setVisible(windows)
        for cb in (getattr(self, "eac", None), getattr(self, "battleye", None)):
            if cb is not None:
                cb.setEnabled(windows)
        self.version.clear()
        if runner == "proton":
            vs = proton_versions()
            d = default_proton(self.s)
            dname = proton_label(os.path.basename(d)) if d else "none installed"
            self.version.addItem(f"Default ({dname})", "")
        elif runner == "wine":
            vs = wine_versions()
            d = default_wine(self.s)
            dname = next((v["name"] for v in vs if v["path"] == d), "none installed")
            self.version.addItem(f"Default ({dname})", "")
        else:
            vs = []
        for v in vs:
            self.version.addItem(v["name"], v["path"])
        cur = self.e["version"] if (initial or runner == self.e["runner"]) else ""
        if cur:
            i = self.version.findData(cur)
            if i < 0:
                self.version.addItem(f"{os.path.basename(cur)} (missing)", cur)
                i = self.version.count() - 1
            self.version.setCurrentIndex(i)
        self.update_prefix_hint()

    def update_prefix_hint(self):
        tmp = dict(self.e, name=self.name.text().strip() or "game", prefix="")
        self.prefix.setPlaceholderText(f"{prefix_of(tmp, self.s)}  (default)")

    # program
    def browse_exe(self):
        start = os.path.dirname(self.exe.text()) if os.path.isfile(self.exe.text()) else (self.start_dir or HOME)
        path, _ = QFileDialog.getOpenFileName(self, "Choose the program", start,
                                              "Programs (*.exe *.msi *.bat *.lnk *.AppImage *.appimage *.sh "
                                              "*.x86_64 *.x86 *.bin);;All files (*)")
        if path:
            self.exe.setText(path)
            self.exe_changed()

    def exe_changed(self):
        exe = self.exe.text().strip()
        if not exe:
            return
        if not self.name.text().strip() and os.path.isfile(exe):
            self.name.setText(guess_name(exe))
        if os.path.isfile(exe):
            want = self.s["windows_runner"] if is_windows(exe) else "native"
            if (want == "native") != (self.runner == "native"):
                self.set_runner(want)
            if is_windows(exe) and not self.e["icon"] and self.icon_src is None and not self.icon_cleared:
                self.icon_src = ("exe", exe)
                self.update_previews()
            if self.new and not self.vr.isChecked() and looks_vr(exe):
                self.vr.setChecked(True)
            if self.new and is_windows(exe):
                found = looks_anticheat(exe)
                self.eac.setChecked(self.eac.isChecked() or found["eac"])
                self.battleye.setChecked(self.battleye.isChecked() or found["battleye"])

    def browse_prefix(self):
        path = QFileDialog.getExistingDirectory(self, "Choose the prefix folder",
                                                self.prefix.text() or os.path.expanduser(self.s["prefix_root"]))
        if path:
            self.prefix.setText(path)

    def browse_cwd(self):
        path = QFileDialog.getExistingDirectory(self, "Start in", self.cwd.text() or HOME)
        if path:
            self.cwd.setText(path)

    # pictures
    def preview_entry(self):
        return dict(self.e, name=self.name.text().strip() or "?", cover="", icon="")

    def current_icon_pix(self):
        if self.icon_cleared:
            return QPixmap()
        if self.icon_src:
            kind, src = self.icon_src
            if kind == "exe":
                data = exe_icon(src)
                pix = QPixmap()
                if data:
                    pix.loadFromData(data)
                return pix
            return QPixmap(src)
        return QPixmap(os.path.join(ICONS, self.e["icon"])) if self.e["icon"] else QPixmap()

    def update_previews(self):
        name = self.name.text().strip() or "?"
        icon = self.current_icon_pix()
        cover = QPixmap()
        if self.cover_data:
            cover.loadFromData(self.cover_data)
        elif self.e["cover"] and not self.cover_cleared:
            cover = QPixmap(os.path.join(COVERS, self.e["cover"]))
        if not cover.isNull():
            self.cover.setPixmap(rounded(cover, 180, 270, 12))
        else:
            self.cover.setPixmap(placeholder(name, 180, 270, icon if not icon.isNull() else None, 12))
        if not icon.isNull():
            self.icon.setPixmap(rounded(icon, 40, 40, 8, fit=True))
        else:
            self.icon.setPixmap(placeholder(name, 40, 40, None, 8))

    def find_cover(self):
        d = CoverSearch(self, self.name.text().strip())
        if d.exec() and d.data:
            self.cover_data = d.data
            self.cover_cleared = False
            self.update_previews()

    def choose_cover(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a cover (portrait works best)", HOME, IMAGE_FILTER)
        if path:
            try:
                with open(path, "rb") as f:
                    self.cover_data = f.read()
                self.cover_cleared = False
            except OSError:
                pass
            self.update_previews()

    def clear_cover(self):
        self.cover_data = None
        self.cover_cleared = True
        self.update_previews()

    def icon_from_exe(self):
        exe = self.exe.text().strip()
        if not (os.path.isfile(exe) and exe_icon(exe)):
            QMessageBox.information(self, "No icon", "This program has no icon to take (only Windows .exe files do).")
            return
        self.icon_src = ("exe", exe)
        self.icon_cleared = False
        self.update_previews()

    def choose_icon(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose an icon", HOME, IMAGE_FILTER)
        if path:
            self.icon_src = ("file", path)
            self.icon_cleared = False
            self.update_previews()

    def clear_icon(self):
        self.icon_src = None
        self.icon_cleared = True
        self.update_previews()

    # saving
    def save(self):
        name = self.name.text().strip()
        exe = os.path.expanduser(self.exe.text().strip())
        if not name:
            self.name.setFocus()
            return QMessageBox.warning(self, "Name missing", "Give it a name.")
        if not exe:
            self.exe.setFocus()
            return QMessageBox.warning(self, "Program missing", "Pick the program to run.")
        if self.runner != "native" and not os.path.isfile(exe):
            return QMessageBox.warning(self, "Program not found", f"{exe} doesn't exist.")
        if self.runner == "native" and not os.path.isfile(exe):
            try:
                first = shlex.split(exe)[0]
            except (ValueError, IndexError):
                first = ""
            if not (first and (os.path.isfile(first) or which(first))):
                return QMessageBox.warning(self, "Program not found",
                                           f"{exe} isn't a file, and there's no command called {first or exe}.")
        e = self.e
        try:
            parse_launch_options(self.args.text())
        except LaunchError as ex:
            self.args.setFocus()
            return QMessageBox.warning(self, "Launch options", str(ex))
        e.update(name=name, exe=exe, args=self.args.text().strip(), runner=self.runner, vr=self.vr.isChecked(),
                 kind=next(k for k, b in self.kind_buttons.items() if b.isChecked()),
                 version=(self.version.currentData() or "") if self.runner != "native" else "",
                 prefix=self.prefix.text().strip(), cwd=self.cwd.text().strip(),
                 wrapper=self.wrapper.text().strip(), env=self.env.toPlainText().strip(),
                 mangohud=self.mangohud.isChecked(), gamemode=self.gamemode.isChecked(),
                 eac=self.eac.isChecked(), battleye=self.battleye.isChecked(),
                 favorite=self.favorite.isChecked(), nsfw=self.nsfw.isChecked(), hidden=self.hidden.isChecked())
        if self.runner != "native" and not e["prefix"]:
            # fixed at creation, so renaming it or changing the prefixes folder later keeps its saves
            e["prefix"] = prefix_of(dict(e, prefix=""), self.s)
        try:
            if self.cover_data:
                old = e["cover"]
                e["cover"] = save_cover(e["id"], self.cover_data)
                remove_file(COVERS, old)
            elif self.cover_cleared:
                remove_file(COVERS, e["cover"])
                e["cover"] = ""
        except (OSError, ValueError) as ex:
            QMessageBox.warning(self, "Cover not saved", f"The cover couldn't be saved: {ex}")
        old_icon = e["icon"]
        if self.icon_cleared:
            e["icon"] = ""
        elif self.icon_src:
            kind, src = self.icon_src
            if kind == "exe":
                e["icon"] = save_exe_icon(e["id"], src) or old_icon
            else:
                img = QImage(src)
                if not img.isNull():
                    os.makedirs(ICONS, exist_ok=True)
                    e["icon"] = f"{e['id']}-{int(time.time())}.png"
                    img.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation).save(os.path.join(ICONS, e["icon"]))
        elif self.new and is_windows(exe):
            e["icon"] = save_exe_icon(e["id"], exe)
        if e["icon"] != old_icon:
            remove_file(ICONS, old_icon)
        put_entry(e)
        refresh_shortcuts(e)
        want = self.desktop_btn.isChecked()
        if want != has_shortcut(e, "desktop"):
            try:
                make_shortcut(e, "desktop") if want else remove_shortcut(e, "desktop")
            except OSError as ex:
                QMessageBox.warning(self, "Hangar", f"Couldn't make the desktop shortcut: {ex}")
        self.accept()


def remove_file(folder, name):
    if name:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass


class SettingsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.win = parent
        self.s = settings()
        self.accent = self.s["accent"]
        self.task = None
        self.setWindowTitle("Hangar settings")
        self.resize(680, 680)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        w = QWidget()
        w.setObjectName("sw")
        w.setStyleSheet("#sw { background: transparent; }")
        scroll.viewport().setObjectName("svp")
        scroll.viewport().setStyleSheet("#svp { background: transparent; }")
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 8, 0)
        v.setSpacing(14)
        scroll.setWidget(w)
        outer.addWidget(scroll, 1)

        def panel(title):
            f = QFrame()
            f.setObjectName("panel")
            lay = QFormLayout(f)
            lay.setContentsMargins(16, 14, 16, 14)
            lay.setSpacing(10)
            lay.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lay.addRow(label(title, "h2"))
            v.addWidget(f)
            return lay

        # appearance
        look = panel("Appearance")
        self.theme = QComboBox()
        for name, t in THEMES.items():
            sw = QPixmap(28, 16)
            sw.fill(Qt.GlobalColor.transparent)
            p = QPainter(sw)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QColor(t["border"]))
            p.setBrush(QColor(t["bg"]))
            p.drawRoundedRect(QRectF(0.5, 0.5, 27, 15), 4, 4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t["accent"]))
            p.drawEllipse(QRectF(15, 4, 8, 8))
            p.end()
            self.theme.addItem(QIcon(sw), name)
        self.theme.setCurrentText(self.s["theme"] if self.s["theme"] in THEMES else "Midnight")
        self.theme.currentTextChanged.connect(self.preview)
        look.addRow("Theme", self.theme)
        arow = QHBoxLayout()
        self.accent_btn = button("", self.pick_accent)
        self.accent_btn.setFixedWidth(120)
        arow.addWidget(self.accent_btn)
        arow.addWidget(button("Theme's own", self.reset_accent))
        arow.addStretch(1)
        look.addRow("Accent", arow)
        self.size = QComboBox()
        for k in SIZES:
            self.size.addItem(k.capitalize(), k)
        self.size.setCurrentIndex(self.size.findData(self.s["size"]))
        look.addRow("Grid covers", self.size)
        self.close_to_tray = QCheckBox("Closing the window keeps Hangar running in the tray")
        self.close_to_tray.setChecked(self.s["close_to_tray"])
        tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
        self.close_to_tray.setToolTip("The X hides Hangar to the system tray (click its icon there to open it again, "
                                      "or right-click → Quit). " + ("" if tray_ok else
                                                                   "No system tray was found, so the X quits for now."))
        look.addRow("", self.close_to_tray)

        # running
        run = panel("Running Windows programs")
        self.win_runner = QComboBox()
        self.win_runner.addItem("Proton", "proton")
        self.win_runner.addItem("Wine", "wine")
        self.win_runner.setCurrentIndex(self.win_runner.findData(self.s["windows_runner"]))
        run.addRow("New .exe files use", self.win_runner)
        self.proton = QComboBox()
        run.addRow("Default Proton", self.proton)
        grow = QHBoxLayout()
        self.ge_btn = button("Get the newest GE-Proton", lambda: self.get_proton("ge"))
        grow.addWidget(self.ge_btn)
        self.dw_btn = button("Get the newest DWProton", lambda: self.get_proton("dw"))
        self.dw_btn.setToolTip("DWProton (An Anime Team, built on Proton-CachyOS): fixes for anime games")
        grow.addWidget(self.dw_btn)
        self.ge_bar = QProgressBar()
        self.ge_bar.setFixedHeight(10)
        self.ge_bar.hide()
        grow.addWidget(self.ge_bar, 1)
        run.addRow("", grow)
        self.ge_note = label(f"Goes into {proton_install_dir()}", "muted", wrap=True)
        run.addRow("", self.ge_note)
        self.wine = QComboBox()
        run.addRow("Default Wine", self.wine)
        self.fill_runners()
        prow = QHBoxLayout()
        self.prefix_root = QLineEdit(self.s["prefix_root"])
        prow.addWidget(self.prefix_root, 1)
        prow.addWidget(button("Browse…", self.browse_root))
        run.addRow("New prefixes go in", prow)
        umu = which("umu-run")
        self.wayland = QCheckBox("Run Proton games as Wayland windows (not VR games)")
        self.wayland.setChecked(self.s["wayland"])
        self.wayland.setToolTip("GE-Proton's Wayland mode (PROTON_ENABLE_WAYLAND=1). Windows then move between "
                                "monitors properly; through X11 they can vanish on a monitor above or left of the "
                                "main one. For one game, put PROTON_ENABLE_WAYLAND=0 in its Environment to turn it "
                                "off.")
        run.addRow("", self.wayland)
        self.exe_opener = QCheckBox("Open .exe files with Hangar (double-click one to run it)")
        self.exe_opener.setChecked(self.s["exe_opener"])
        self.exe_opener.setToolTip("Hangar becomes the app for Windows programs: opening one from your file manager "
                                   "shows a small window to run it with Proton or Wine, and add it to the library "
                                   "if you like.")
        run.addRow("", self.exe_opener)
        self.use_umu = QCheckBox("Use umu-launcher for Proton" + ("" if umu else " (not installed)"))
        self.use_umu.setChecked(self.s["use_umu"])
        self.use_umu.setToolTip("umu runs Proton inside Steam's runtime, like Steam does. Without it, Proton runs "
                                "directly, which works for most games.")
        run.addRow("", self.use_umu)
        wt = which("winetricks")
        wrow = QHBoxLayout()
        self.wt_note = label("Winetricks: " + (wt if wt else "not installed"), "muted")
        wrow.addWidget(self.wt_note, 1)
        if not wt:
            self.wt_btn = button("Download winetricks", self.get_winetricks)
            wrow.addWidget(self.wt_btn)
        run.addRow("", wrow)

        # content
        content = panel("Content")
        self.show_nsfw = QCheckBox("Show NSFW entries in Library and Favourites too")
        self.show_nsfw.setChecked(self.s["show_nsfw"])
        content.addRow("", self.show_nsfw)
        self.blur_nsfw = QCheckBox("Blur NSFW covers (point at one to see it)")
        self.blur_nsfw.setChecked(self.s["blur_nsfw"])
        content.addRow("", self.blur_nsfw)
        self.hide_nsfw = QCheckBox("Hide the NSFW tab: only a key combination shows it")
        self.hide_nsfw.setChecked(self.s["nsfw_hidden"])
        content.addRow("", self.hide_nsfw)
        krow = QHBoxLayout()
        self.nsfw_key = QKeySequenceEdit(QKeySequence(self.s["nsfw_key"] or "Ctrl+Shift+N"))
        self.nsfw_key.setMaximumSequenceLength(1)
        self.nsfw_key.setToolTip("Shows (or hides again) the NSFW tab. Without “Hide the NSFW tab”, it jumps to it")
        krow.addWidget(self.nsfw_key, 1)
        krow.addWidget(button("Reset", lambda: self.nsfw_key.setKeySequence(QKeySequence("Ctrl+Shift+N"))))
        content.addRow("NSFW tab key", krow)
        prow = QHBoxLayout()
        self.pass_note = label("", "muted")
        prow.addWidget(self.pass_note, 1)
        self.pass_set = button("", self.set_nsfw_password)
        prow.addWidget(self.pass_set)
        self.pass_off = button("Remove", self.remove_nsfw_password)
        prow.addWidget(self.pass_off)
        content.addRow("NSFW password", prow)
        self.show_password_state()
        grow2 = QHBoxLayout()
        self.games_root = QLineEdit(self.s["games_root"])
        grow2.addWidget(self.games_root, 1)
        grow2.addWidget(button("Browse…", self.browse_games))
        content.addRow("Dropped games go in", grow2)

        vr = panel("VR (WiVRn)")
        self.vr_options = QLineEdit(self.s["vr_options"])
        self.vr_options.setPlaceholderText("Optional, e.g. PRESSURE_VESSEL_IMPORT_OPENXR_1_RUNTIMES=1 %command%")
        self.vr_options.setToolTip("Added to the launch options of every game marked VR (Steam syntax)")
        vr.addRow("VR launch options", self.vr_options)
        self.start_wivrn = QCheckBox("Start the WiVRn server before a VR game if it isn't running")
        self.start_wivrn.setChecked(self.s["start_wivrn"])
        self.start_wivrn.setEnabled(bool(which("wivrn-server")) or self.s["start_wivrn"])
        vr.addRow("", self.start_wivrn)
        xr = xrizer_dir()
        wj = next((j for j in WIVRN_JSON if os.path.isfile(j)), "")
        vr.addRow("", label("WiVRn: " + ("installed" if which("wivrn-server") else "not installed") +
                            (", running" if wivrn_running() else "") +
                            f" · OpenXR runtime: {'WiVRn' if wj else 'not found'}" +
                            f" · OpenVR games: {'xrizer (' + xr + ')' if xr else 'no xrizer/OpenComposite found'}",
                            "muted", wrap=True))

        covers = panel("Covers")
        self.sgdb = QLineEdit(self.s["sgdb_key"])
        self.sgdb.setPlaceholderText("Optional")
        self.sgdb.setEchoMode(QLineEdit.EchoMode.Password)
        covers.addRow("SteamGridDB key", self.sgdb)
        note = label('For covers of games that aren\'t on Steam. Get a free key at '
                     '<a href="https://www.steamgriddb.com/profile/preferences/api">steamgriddb.com</a> '
                     '(Preferences → API).', "muted", wrap=True)
        note.setOpenExternalLinks(False)
        note.linkActivated.connect(open_path)
        covers.addRow("", note)
        urow = QHBoxLayout()
        urow.addWidget(label(f"Hangar {VERSION} · library in {CONF_DIR}", "muted", wrap=True), 1)
        self.update_btn = button("Check for updates", self.check_updates)
        urow.addWidget(self.update_btn)
        v.addLayout(urow)
        self.update_bar = QProgressBar()
        self.update_bar.setFixedHeight(10)
        self.update_bar.hide()
        v.addWidget(self.update_bar)
        self.update_note = label("", "muted", wrap=True)
        self.update_note.setOpenExternalLinks(False)
        self.update_note.linkActivated.connect(open_path)
        self.update_note.hide()
        v.addWidget(self.update_note)
        self.update_info = None
        v.addStretch(1)

        buttons = QDialogButtonBox()
        ok = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setObjectName("primary")
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.update_accent()

    def fill_runners(self):
        self.proton.clear()
        for v in proton_versions():
            self.proton.addItem(v["name"], v["path"])
        if not self.proton.count():
            self.proton.addItem("None installed", "")
        i = self.proton.findData(default_proton(self.s))
        self.proton.setCurrentIndex(max(0, i))
        self.wine.clear()
        for v in wine_versions():
            self.wine.addItem(v["name"], v["path"])
        if not self.wine.count():
            self.wine.addItem("None installed", "")
        i = self.wine.findData(default_wine(self.s))
        self.wine.setCurrentIndex(max(0, i))

    def update_accent(self):
        t = current_theme(dict(self.s, theme=self.theme.currentText(), accent=self.accent))
        self.accent_btn.setText(f"Pick…  {t['accent']}" if self.accent else "Pick…")
        self.accent_btn.setStyleSheet(f"QPushButton {{ background: {t['accent']}; color: {t['on_accent']}; "
                                      "border: none; font-weight: 700; }")

    def preview(self, *_):
        apply_theme(QApplication.instance(), dict(self.s, theme=self.theme.currentText(), accent=self.accent))
        self.update_accent()

    def pick_accent(self):
        c = QColorDialog.getColor(QColor(current_theme(dict(self.s, theme=self.theme.currentText(),
                                                            accent=self.accent))["accent"]), self, "Accent colour")
        if c.isValid():
            self.accent = c.name()
            self.preview()

    def reset_accent(self):
        self.accent = ""
        self.preview()

    def browse_games(self):
        path = QFileDialog.getExistingDirectory(self, "Dropped games go in",
                                                os.path.expanduser(self.games_root.text()))
        if path:
            self.games_root.setText(path)

    def browse_root(self):
        path = QFileDialog.getExistingDirectory(self, "New prefixes go in", os.path.expanduser(self.prefix_root.text()))
        if path:
            self.prefix_root.setText(path)

    def get_proton(self, source):
        self.ge_btn.setEnabled(False)
        self.dw_btn.setEnabled(False)
        self.ge_bar.show()
        self.ge_bar.setRange(0, 0)
        self.ge_note.setText("Looking for the newest release…")
        self.task = Task(lambda report: install_proton(source, report), self)
        self.task.progress.connect(self.ge_progress)
        self.task.done.connect(lambda tag, error: self.proton_done(source, tag, error))
        self.task.start()

    def ge_progress(self, info):
        stage, done, total, tag = info
        if stage == "download" and total:
            self.ge_bar.setRange(0, 1000)
            self.ge_bar.setValue(int(done * 1000 / total))
            self.ge_note.setText(f"Downloading {tag}: {done >> 20} / {total >> 20} MB")
        elif stage == "extract":
            self.ge_bar.setRange(0, 0)
            self.ge_note.setText(f"Unpacking {tag}…")

    def update_say(self, text):
        self.update_note.setText(text)
        self.update_note.show()

    def check_updates(self):
        if self.update_info and self.update_info.get("ready"):
            return restart_hangar()
        if self.update_info and self.update_info["newer"] and self.update_info["url"] and APPIMAGE:
            return self.download_update()
        self.update_btn.setEnabled(False)
        self.update_say("Checking GitHub…")
        self.update_task = Task(check_update, self)
        self.update_task.done.connect(self.update_checked)
        self.update_task.start()

    def update_checked(self, info, error):
        self.update_btn.setEnabled(True)
        if error:
            return self.update_say(f"Couldn't check for updates: {error}")
        self.update_info = info
        page = f'<a href="{info["page"]}">release page</a>'
        if not info["newer"]:
            return self.update_say(f"Hangar {VERSION} is the newest version.")
        if info["url"] and APPIMAGE:
            self.update_btn.setText(f"Update to {info['version']}")
            self.update_say(f"Hangar {info['version']} is out ({page}).")
        else:
            self.update_say(f"Hangar {info['version']} is out: get it from the {page}.")

    def download_update(self):
        self.update_btn.setEnabled(False)
        self.update_bar.show()
        self.update_bar.setRange(0, 0)
        info = self.update_info
        self.update_task = Task(lambda report: install_update(info, report), self)
        self.update_task.progress.connect(self.update_progress)
        self.update_task.done.connect(self.update_done)
        self.update_task.start()

    def update_progress(self, p):
        _stage, done, total, ver = p
        if total:
            self.update_bar.setRange(0, 1000)
            self.update_bar.setValue(int(done * 1000 / total))
        self.update_say(f"Downloading Hangar {ver}: {done >> 20} / {total >> 20} MB")

    def update_done(self, ver, error):
        self.update_btn.setEnabled(True)
        self.update_bar.hide()
        if error:
            return self.update_say(f"Couldn't update: {error}")
        self.update_info["ready"] = True
        self.update_btn.setText("Restart Hangar")
        self.update_say(f"Hangar {ver} is installed. Restart Hangar to use it (unsaved settings here are lost).")

    def proton_done(self, source, tag, error):
        self.ge_btn.setEnabled(True)
        self.dw_btn.setEnabled(True)
        self.ge_bar.hide()
        if error:
            self.ge_note.setText(f"Couldn't get {PROTON_SOURCES[source][0]}: {error}")
            return
        if source != "ge":  # only GE-Proton becomes the default; others are picked per game (or here)
            self.fill_runners()
            self.ge_note.setText(f"{tag} is installed: pick it for a game in its Proton version list, "
                                 "or as the default above.")
            return
        self.ge_note.setText(f"{tag} is installed and is now the default.")
        self.s["proton"] = ""
        self.fill_runners()
        i = next((i for i in range(self.proton.count()) if self.proton.itemText(i) == tag), -1)
        if i >= 0:
            self.proton.setCurrentIndex(i)

    def get_winetricks(self):
        self.wt_btn.setEnabled(False)
        self.wt_note.setText("Downloading winetricks…")
        self.wt_task = Task(install_winetricks, self)
        self.wt_task.done.connect(lambda path, err: (
            self.wt_note.setText(f"Winetricks: {path}" if path else f"Download failed: {err}"),
            self.wt_btn.setVisible(not path), self.wt_btn.setEnabled(True)))
        self.wt_task.start()

    # NSFW password: changed at once (the old one is asked first), not with Save
    def show_password_state(self):
        on = bool((settings().get("nsfw_pass") or {}).get("hash"))
        self.pass_note.setText("On: the NSFW tab asks for it" if on else "Off")
        self.pass_set.setText("Change…" if on else "Set a password…")
        self.pass_off.setVisible(on)

    def check_old_password(self):
        rec = settings().get("nsfw_pass") or {}
        if not rec.get("hash"):
            return True
        pw, ok = QInputDialog.getText(self, "NSFW password", "Current password:", QLineEdit.EchoMode.Password)
        if not ok:
            return False
        if not password_ok(pw, rec):
            QMessageBox.warning(self, "NSFW password", "That's not the password.")
            return False
        return True

    def set_nsfw_password(self):
        if not self.check_old_password():
            return
        pw, ok = QInputDialog.getText(self, "NSFW password", "New password:", QLineEdit.EchoMode.Password)
        if not ok or not pw:
            return
        again, ok = QInputDialog.getText(self, "NSFW password", "The same password again:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        if again != pw:
            QMessageBox.warning(self, "NSFW password", "The two didn't match, so the password wasn't changed.")
            return
        save_settings(nsfw_pass=hash_password(pw))
        self.show_password_state()

    def remove_nsfw_password(self):
        if self.check_old_password():
            save_settings(nsfw_pass={})
            self.show_password_state()

    def save(self):
        save_settings(nsfw_hidden=self.hide_nsfw.isChecked(),
                      nsfw_key=self.nsfw_key.keySequence().toString() or "Ctrl+Shift+N")
        save_settings(theme=self.theme.currentText(), accent=self.accent, size=self.size.currentData(),
                      windows_runner=self.win_runner.currentData(), proton=self.proton.currentData() or "",
                      wine=self.wine.currentData() or "",
                      prefix_root=self.prefix_root.text().strip() or DEFAULT_SETTINGS["prefix_root"],
                      use_umu=self.use_umu.isChecked(), show_nsfw=self.show_nsfw.isChecked(),
                      vr_options=self.vr_options.text().strip(), start_wivrn=self.start_wivrn.isChecked(),
                      wayland=self.wayland.isChecked(), exe_opener=self.exe_opener.isChecked(),
                      close_to_tray=self.close_to_tray.isChecked(),
                      blur_nsfw=self.blur_nsfw.isChecked(), sgdb_key=self.sgdb.text().strip(),
                      games_root=self.games_root.text().strip() or DEFAULT_SETTINGS["games_root"])
        self.accept()

    def done(self, r):
        for t in (self.task, getattr(self, "wt_task", None)):
            if t and t.isRunning():
                t.setParent(None)
                RUNNING_TASKS.append(t)
                t.finished.connect(lambda t=t: RUNNING_TASKS.remove(t) if t in RUNNING_TASKS else None)
                try:
                    t.progress.disconnect()
                    t.done.disconnect()
                except TypeError:
                    pass
        if r != QDialog.DialogCode.Accepted.value:
            apply_theme(QApplication.instance())
        super().done(r)


# ---- library views -----------------------------------------------------------------------------

def entry_menu(win, e, parent):
    m = QMenu(parent)
    running = win.launcher.is_running(e["id"])
    m.addAction("Stop" if running else "Play", lambda: win.play(e["id"]))
    m.addAction("Edit…", lambda: win.edit(e["id"]))
    m.addAction("Unfavourite" if e["favorite"] else "Favourite", lambda: win.mark(e["id"], favorite=not e["favorite"]))
    m.addSeparator()
    target = e["exe"] if os.path.exists(e["exe"]) else ""
    a = m.addAction("Open its folder", lambda: open_path(os.path.dirname(target) if os.path.isfile(target) else target))
    a.setEnabled(bool(target))
    if e["runner"] != "native":
        wine = m.addMenu("Wine / Proton")
        pfx = prefix_of(e)
        a = wine.addAction("Open the prefix folder", lambda: open_path(pfx))
        a.setEnabled(os.path.isdir(pfx))
        wine.addAction("Open C: drive", lambda: open_path(os.path.join(wine_prefix(e), "drive_c")))
        wine.addSeparator()
        wine.addAction("Wine settings (winecfg)", lambda: win.run_tool(e, "winecfg"))
        wine.addAction("Run an .exe in this prefix…", lambda: win.run_in_prefix(e))
        wt = which("winetricks")
        a = wine.addAction("Winetricks" if wt else "Winetricks (get it in Settings)", lambda: win.winetricks(e))
        a.setEnabled(bool(wt))
        wine.addAction("Registry editor", lambda: win.run_tool(e, "regedit"))
        wine.addSeparator()
        wine.addAction("Stop everything in this prefix", lambda: (kill_prefix(e), win.say("Stopped the prefix.")))
    m.addAction("Show the last log", lambda: win.show_log(e))
    m.addSeparator()
    m.addAction("Not NSFW" if e["nsfw"] else "Mark as NSFW", lambda: win.mark(e["id"], nsfw=not e["nsfw"]))
    m.addAction("Unhide" if e["hidden"] else "Hide", lambda: win.mark(e["id"], hidden=not e["hidden"]))
    other = "app" if e["kind"] == "game" else "game"
    m.addAction(f"Move to {KINDS[other]}", lambda: win.mark(e["id"], kind=other))
    has = has_shortcut(e)
    m.addAction("Remove from app menu" if has else "Add to app menu", lambda: win.toggle_shortcut(e["id"]))
    has = has_shortcut(e, "desktop")
    m.addAction("Remove desktop shortcut" if has else "Make a desktop shortcut",
                lambda: win.toggle_shortcut(e["id"], "desktop"))
    m.addSeparator()
    m.addAction("Remove…", lambda: win.remove(e["id"]))
    return m


def more_button(win, e):
    more = QToolButton()
    more.setText("⋯")
    more.setCursor(Qt.CursorShape.PointingHandCursor)
    more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    more.setMenu(entry_menu(win, e, more))
    return more


class EntryWidget(QFrame):
    """shared: right-click menu, double-click to play, drop an image to set the cover"""

    def __init__(self, win, e):
        super().__init__()
        self.win, self.e = win, e
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: entry_menu(win, e, self).exec(self.mapToGlobal(pos)))

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.win.play(self.e["id"])

    def dragEnterEvent(self, ev):
        if any(u.isLocalFile() and os.path.exists(u.toLocalFile()) for u in ev.mimeData().urls()):
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile() and os.path.exists(u.toLocalFile())]
        ev.acceptProposedAction()
        if len(paths) == 1 and paths[0].lower().endswith(IMAGE_EXT):
            self.win.set_cover_file(self.e["id"], paths[0])
        elif paths:
            QTimer.singleShot(0, lambda: self.win.add_to_game_folder(self.e["id"], paths))


def play_button(win, e, round_=False):
    running = win.launcher.is_running(e["id"])
    if round_:
        b = button("■" if running else "▶", lambda: win.play(e["id"]), "roundStop" if running else "round",
                   "Stop" if running else "Play")
        b.setFixedSize(38, 38)
    else:
        b = button("■  Stop" if running else "▶  Play", lambda: win.play(e["id"]), "stop" if running else "primary")
        b.setMinimumWidth(92)
    return b


class Card(EntryWidget):
    """one row of the list view"""

    def __init__(self, win, e, blur):
        super().__init__(win, e)
        self.setObjectName("card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 14, 10)
        lay.setSpacing(14)
        thumb = QLabel()
        thumb.setFixedSize(52, 52)
        thumb.setPixmap(picture(e, 52, 52, "icon", blur, 10))
        lay.addWidget(thumb)
        mid = QVBoxLayout()
        mid.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(Elided(("★ " if e["favorite"] else "") + e["name"], "name"), 1)
        mid.addLayout(top)
        badges = QHBoxLayout()
        badges.setSpacing(6)
        if win.launcher.is_running(e["id"]):
            badges.addWidget(label("Running", "badgeRun"))
        badges.addWidget(label(runner_label(e, win.s), "badgeAccent" if e["runner"] != "native" else "badge"))
        if e["nsfw"]:
            badges.addWidget(label("NSFW", "badgeNsfw"))
        if e["vr"]:
            badges.addWidget(label("VR", "badgeAccent"))
        if e["hidden"]:
            badges.addWidget(label("Hidden", "badge"))
        played = f"Played {human_time(e['playtime'])}" if e["playtime"] else (
            "Played briefly" if e["last_played"] else "Not played yet")
        if e["last_played"]:
            played += f" · last {human_when(e['last_played'])}"
        badges.addWidget(label(played, "muted"))
        badges.addStretch(1)
        mid.addLayout(badges)
        lay.addLayout(mid, 1)
        lay.addWidget(play_button(win, e))
        lay.addWidget(more_button(win, e))


class Tile(EntryWidget):
    """one cover of the grid view"""

    def __init__(self, win, e, blur, w):
        super().__init__(win, e)
        self.setObjectName("tile")
        h = int(w * 1.5)
        self.setFixedWidth(w)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        holder = QWidget()
        holder.setFixedSize(w, h)
        self.cover = QPushButton(holder)
        self.cover.setObjectName("cover")
        self.cover.setGeometry(0, 0, w, h)
        self.soft = QIcon(picture(e, w - 4, h - 4, "cover", blur, 10))
        # blurred covers show themselves while you point at them
        self.sharp = QIcon(picture(e, w - 4, h - 4, "cover", False, 10)) if blur else None
        self.cover.setIcon(self.soft)
        self.cover.setIconSize(QSize(w - 4, h - 4))
        self.cover.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cover.setToolTip("Double-click to play, right-click for more")
        self.cover.installEventFilter(self)
        pb = play_button(win, e, round_=True)
        pb.setParent(holder)
        pb.move(w - pb.width() - 10, h - pb.height() - 10)
        if win.launcher.is_running(e["id"]):
            run = label("Running", "badgeRun")
            run.setParent(holder)
            run.adjustSize()
            run.move(10, 10)
        lay.addWidget(holder)
        row = QHBoxLayout()
        row.setSpacing(2)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        texts.addWidget(Elided(("★ " if e["favorite"] else "") + e["name"], "name"))
        sub = ("VR · " if e["vr"] else "") + runner_label(e, win.s)
        if e["playtime"]:
            sub += f" · {human_time(e['playtime'])}"
        texts.addWidget(Elided(sub, "muted"))
        row.addLayout(texts, 1)
        row.addWidget(more_button(win, e), 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(row)

    def eventFilter(self, obj, ev):
        if obj is self.cover:
            if self.sharp is not None and ev.type() in (ev.Type.Enter, ev.Type.Leave):
                self.cover.setIcon(self.sharp if ev.type() == ev.Type.Enter else self.soft)
            if ev.type() == ev.Type.MouseButtonDblClick and ev.button() == Qt.MouseButton.LeftButton:
                self.win.play(self.e["id"])
                return True
            if ev.type() == ev.Type.MouseButtonPress and ev.button() == Qt.MouseButton.RightButton:
                entry_menu(self.win, self.e, self).exec(ev.globalPosition().toPoint())
                return True
        return super().eventFilter(obj, ev)


class DropOverlay(QFrame):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("drop")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay = QVBoxLayout(self)
        t = label("Drop to add it", "title")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        self.hide()


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hangar")
        self.resize(1100, 720)
        self.setMinimumSize(700, 460)
        self.setAcceptDrops(True)
        self.s = settings()
        self.launcher = Launcher()
        self.launcher.changed.connect(self.refresh)
        self.launcher.finished.connect(self.on_finished)
        self.cols = 0
        self.entries = []

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(22, 18, 22, 12)
        v.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(12)
        icon = QLabel()
        icon.setPixmap(QIcon(ICON).pixmap(42, 42) if os.path.exists(ICON) else QPixmap())
        head.addWidget(icon)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        titles.addWidget(label("Hangar", "title"))
        self.subtitle = label("", "muted")
        titles.addWidget(self.subtitle)
        head.addLayout(titles)
        head.addStretch(1)
        kbox, self.kind_buttons = segments(KINDS, self.s["kind"], self.set_kind)
        head.addWidget(kbox)
        head.addSpacing(6)
        self.add_btn = button("", self.add, "primary")
        head.addWidget(self.add_btn)
        gear = QToolButton()
        gear.setText("⚙")
        gear.setToolTip("Settings")
        gear.setCursor(Qt.CursorShape.PointingHandCursor)
        gear.clicked.connect(self.open_settings)
        head.addWidget(gear)
        v.addLayout(head)

        bar = QHBoxLayout()
        bar.setSpacing(10)
        tbox, self.tab_buttons = segments(TABS, self.s["tab"], self.set_tab)
        bar.addWidget(tbox)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        bar.addWidget(self.search, 1)
        self.sort = QComboBox()
        for k, text in SORTS.items():
            self.sort.addItem(text, k)
        self.sort.setCurrentIndex(max(0, self.sort.findData(self.s["sort"])))
        self.sort.currentIndexChanged.connect(lambda: (save_settings(sort=self.sort.currentData()), self.refresh()))
        bar.addWidget(self.sort)
        vbox, self.view_buttons = segments({"list": "☰  List", "grid": "▦  Grid"}, self.s["view"], self.set_view)
        bar.addWidget(vbox)
        v.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.holder = QWidget()
        self.holder.setObjectName("holder")
        self.holder.setStyleSheet("#holder { background: transparent; }")
        self.scroll.viewport().setObjectName("viewport")
        self.scroll.viewport().setStyleSheet("#viewport { background: transparent; }")
        self.items = QVBoxLayout(self.holder)
        self.items.setContentsMargins(0, 0, 6, 0)
        self.items.setSpacing(8)
        self.scroll.setWidget(self.holder)
        v.addWidget(self.scroll, 1)

        self.tip = ("Tip: drop a program, folder or archive (.zip/.rar/.7z) on this window to add it · "
                    "drop files on a game to put them in its folder")
        self.status = label(self.tip, "muted")
        srow = QHBoxLayout()
        srow.addWidget(self.status, 1)
        self.busy_bar = QProgressBar()
        self.busy_bar.setFixedSize(220, 10)
        self.busy_bar.hide()
        srow.addWidget(self.busy_bar)
        v.addLayout(srow)
        self.tasks = []
        self.status_timer = QTimer(self)
        self.status_timer.setSingleShot(True)
        self.status_timer.timeout.connect(lambda: self.status.setText(self.tip))
        self.overlay = DropOverlay(root)

        QShortcut(QKeySequence("Ctrl+F"), self, self.search.setFocus)
        QShortcut(QKeySequence("Ctrl+N"), self, self.add)
        QShortcut(QKeySequence("F5"), self, self.refresh)
        # NSFW: unlocked until you leave the tab or Hangar is hidden; a hidden tab shows with the key
        self.nsfw_unlocked = False
        self.nsfw_shown = False
        self.nsfw_shortcut = QShortcut(QKeySequence(settings()["nsfw_key"] or "Ctrl+Shift+N"), self,
                                       self.toggle_nsfw_tab)
        self.refresh()

    # helpers
    def say(self, text, secs=7):
        self.status.setText(text)
        self.status_timer.start(secs * 1000)

    def set_kind(self, k):
        save_settings(kind=k)
        self.refresh()

    def set_tab(self, k):
        if k != "nsfw":
            self.nsfw_unlocked = False
        save_settings(tab=k)
        self.refresh()

    # NSFW privacy
    def nsfw_tab_visible(self):
        return not self.s["nsfw_hidden"] or self.nsfw_shown

    def nsfw_locked(self):
        return bool((self.s.get("nsfw_pass") or {}).get("hash")) and not self.nsfw_unlocked

    def nsfw_private(self):
        """NSFW entries are kept out of every other tab and count while the tab is hidden or locked"""
        return not self.nsfw_tab_visible() or self.nsfw_locked()

    def toggle_nsfw_tab(self):
        s = settings()
        if s["nsfw_hidden"]:
            self.nsfw_shown = not self.nsfw_shown
            if not self.nsfw_shown:
                self.nsfw_unlocked = False
                if s["tab"] == "nsfw":
                    save_settings(tab="library")
            else:
                save_settings(tab="nsfw")
        else:
            if s["tab"] == "nsfw":
                self.nsfw_unlocked = False
                save_settings(tab="library")
            else:
                save_settings(tab="nsfw")
        self.refresh()

    def lock_nsfw(self):
        """hiding Hangar locks NSFW again and hides a hidden tab"""
        if self.nsfw_unlocked or self.nsfw_shown:
            self.nsfw_unlocked = False
            self.nsfw_shown = False
            if settings()["tab"] == "nsfw" and (settings()["nsfw_hidden"]):
                save_settings(tab="library")
            self.refresh()

    def try_unlock(self, field, err):
        if password_ok(field.text(), self.s.get("nsfw_pass") or {}):
            self.nsfw_unlocked = True
            self.refresh()
        else:
            field.clear()
            err.setText("That's not the password")
            err.show()

    def lock_panel(self):
        box = QFrame()
        box.setObjectName("panel")
        box.setMaximumWidth(420)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(30, 30, 30, 30)
        lay.setSpacing(12)
        icon = label("🔒", "h2")
        icon.setStyleSheet("font-size: 34px;")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon)
        t = label("NSFW is locked", "h2")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setPlaceholderText("Password")
        lay.addWidget(field)
        err = label("", "muted")
        err.setAlignment(Qt.AlignmentFlag.AlignCenter)
        err.hide()
        lay.addWidget(err)
        go = button("Unlock", lambda: self.try_unlock(field, err), "primary")
        field.returnPressed.connect(go.click)
        lay.addWidget(go)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(box)
        row.addStretch(1)
        self.items.addSpacing(40)
        self.items.addLayout(row)
        self.items.addStretch(1)
        QTimer.singleShot(0, field.setFocus)

    def set_view(self, k):
        save_settings(view=k)
        self.refresh()

    def visible_entries(self):
        s = self.s
        q = self.search.text().strip().lower()
        out = []
        for e in self.entries:
            if e["kind"] != s["kind"]:
                continue
            if q and q not in e["name"].lower() and q not in e["exe"].lower():
                continue
            if not self.in_tab(e, s["tab"]):
                continue
            out.append(e)
        key = s["sort"]
        out.sort(key=lambda e: natural(e["name"]))
        if key == "recent":
            out.sort(key=lambda e: -e["last_played"])
        elif key == "playtime":
            out.sort(key=lambda e: -e["playtime"])
        elif key == "added":
            out.sort(key=lambda e: -e["added"])
        return out

    def in_tab(self, e, tab):
        if e["nsfw"] and tab != "nsfw" and self.nsfw_private():
            return False
        if tab == "hidden":
            return e["hidden"]
        if e["hidden"]:
            return False
        if tab == "nsfw":
            return e["nsfw"]
        if e["nsfw"] and not self.s["show_nsfw"]:
            return False
        return tab == "library" or (tab == "favorites" and e["favorite"]) or (tab == "vr" and e["vr"])

    def columns(self):
        w = SIZES.get(self.s["size"], 172)
        avail = self.scroll.viewport().width() - 8
        return max(1, (avail + 16) // (w + 16))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.overlay.setGeometry(self.centralWidget().rect().adjusted(10, 10, -10, -10))
        if self.s["view"] == "grid" and self.columns() != self.cols:
            QTimer.singleShot(0, self.refresh)

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.refresh)

    def clear(self):
        while self.items.count():
            it = self.items.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                lay = it.layout()
                while lay.count():
                    w = lay.takeAt(0).widget()
                    if w:
                        w.deleteLater()

    def refresh(self):
        self.s = settings()
        self.entries = library()
        s = self.s
        if s["tab"] == "nsfw" and not self.nsfw_tab_visible():
            save_settings(tab="library")
            self.s = s = settings()
        private = self.nsfw_private()
        counted = [e for e in self.entries if not (private and e["nsfw"])]
        for k, b in self.kind_buttons.items():
            b.setChecked(k == s["kind"])
            n = sum(1 for e in counted if e["kind"] == k and not e["hidden"])
            b.setText(f"{KINDS[k]}  {n}" if n else KINDS[k])
        for k, b in self.tab_buttons.items():
            b.setChecked(k == s["tab"])
            if k == "nsfw":
                # never says how many there are
                b.setVisible(self.nsfw_tab_visible())
                b.setText(("🔒  " if self.nsfw_locked() else "") + TABS[k])
                continue
            n = sum(1 for e in self.entries if e["kind"] == s["kind"] and self.in_tab(e, k))
            b.setText(f"{TABS[k]}  {n}" if n and k != "library" else TABS[k])
        self.view_buttons[s["view"]].setChecked(True)
        self.add_btn.setText("+  Add a game" if s["kind"] == "game" else "+  Add an app")
        games = sum(1 for e in counted if e["kind"] == "game")
        apps = len(counted) - games
        running = len(self.launcher.running)
        sub = f"{games} game{'s' * (games != 1)} · {apps} app{'s' * (apps != 1)}"
        if running:
            sub += f" · {running} running"
        self.subtitle.setText(sub)

        vbar = self.scroll.verticalScrollBar().value()
        self.clear()
        if s["tab"] == "nsfw" and self.nsfw_locked():
            self.lock_panel()
            return
        shown = self.visible_entries()
        if not shown:
            self.empty_state()
        elif s["view"] == "grid":
            w = SIZES.get(s["size"], 172)
            self.cols = self.columns()
            grid = QGridLayout()
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(18)
            grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            for i, e in enumerate(shown):
                grid.addWidget(Tile(self, e, self.blur(e), w), i // self.cols, i % self.cols)
            self.items.addLayout(grid)
            self.items.addStretch(1)
        else:
            for e in shown:
                self.items.addWidget(Card(self, e, self.blur(e)))
            self.items.addStretch(1)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(vbar))

    def blur(self, e):
        return e["nsfw"] and self.s["blur_nsfw"]

    def empty_state(self):
        s = self.s
        what = "games" if s["kind"] == "game" else "apps"
        if self.search.text().strip():
            title, text = "Nothing matches", "Try another search."
        elif s["tab"] == "library":
            any_kind = any(e["kind"] == s["kind"] for e in self.entries)
            title = f"No {what} yet" if not any_kind else f"All your {what} are hidden or NSFW"
            text = (f"Add {what} you have outside Steam: Windows .exe files run through Proton or Wine, "
                    "native programs and AppImages run as they are. You can also drop files on this window.")
        elif s["tab"] == "vr":
            title, text = "No VR games", "Tick “VR game” in a game's settings; games that ship VR libraries " \
                                         "get it ticked when you add them."
        elif s["tab"] == "favorites":
            title, text = "No favourites", f"Mark {what} as favourite from their ⋯ menu."
        elif s["tab"] == "nsfw":
            title, text = "Nothing marked NSFW", "Mark entries as NSFW from their ⋯ menu; they then only show here."
        else:
            title, text = "Nothing hidden", "Hidden entries only show in this tab."
        box = QFrame()
        box.setObjectName("panel")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(30, 30, 30, 30)
        lay.setSpacing(10)
        t = label(title, "h2")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        d = label(text, "muted", wrap=True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(d)
        if s["tab"] == "library" and not self.search.text().strip():
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(button("+  Add a game" if s["kind"] == "game" else "+  Add an app", self.add, "primary"))
            if not proton_versions():
                row.addWidget(button("Get GE-Proton", self.open_settings))
            row.addStretch(1)
            lay.addLayout(row)
        self.items.addSpacing(30)
        self.items.addWidget(box)
        self.items.addStretch(1)

    # actions
    def add(self, exe=""):
        d = EditDialog(self, kind=self.s["kind"], exe=exe if isinstance(exe, str) else "")
        if d.exec():
            e = d.e
            if e["kind"] != self.s["kind"]:
                save_settings(kind=e["kind"])
            self.refresh()
            self.say(f"Added {e['name']}.")

    # adding by dropping
    def run_task(self, text, fn, on_done):
        """runs fn(report) in the background with the busy bar; on_done(result, error) afterwards"""
        task = Task(fn, self)
        self.tasks.append(task)
        self.status_timer.stop()
        self.status.setText(text)
        self.busy_bar.setRange(0, 0)
        self.busy_bar.show()

        def progress(info):
            _stage, done, total = info[:3]
            if total and len(self.tasks) == 1:
                self.busy_bar.setRange(0, total)
                self.busy_bar.setValue(done)

        def finished(result, error):
            self.tasks.remove(task)
            if not self.tasks:
                self.busy_bar.hide()
            self.status.setText(self.tip)
            on_done(result, error)

        task.progress.connect(progress)
        task.done.connect(finished)
        task.start()

    def import_paths(self, paths):
        """what dropping on the window (or opening files with Hangar) does"""
        archives = [p for p in paths if os.path.isfile(p) and is_archive(p)]
        folders = [p for p in paths if os.path.isdir(p)]
        programs = [p for p in paths if os.path.isfile(p) and p not in archives and not LATER_PART_RE.search(p)]
        for a in archives[:5]:
            self.import_archive(a)
        for f in folders[:5]:
            self.import_folder(f)
        for p in programs[:5]:
            if is_windows(p) and is_installer(p):
                r = QMessageBox.question(self, "Run the installer?",
                                         f"{os.path.basename(p)} looks like an installer.\n\nRun it in a new prefix "
                                         "now? When it finishes, Hangar finds the installed game and adds it.\n\n"
                                         "No adds the file itself as the program.")
                if r == QMessageBox.StandardButton.Yes:
                    self.run_installer(p, guess_name(p))
                    continue
            self.add(p)

    def import_archive(self, archive):
        name = archive_name(archive)
        root = os.path.expanduser(self.s["games_root"])
        dest = unique_dir(root, name)

        def work(report):
            extract(archive, dest, report)
            flatten(dest)
            return dest

        def done(folder, error):
            if error:
                shutil.rmtree(dest, ignore_errors=True)
                QMessageBox.warning(self, "Couldn't unpack it", f"{os.path.basename(archive)}:\n\n{error}")
                return
            self.say(f"Unpacked into {folder}")
            self.add_from_folder(folder, name)

        self.run_task(f"Unpacking {os.path.basename(archive)} into {dest}…", work, done)

    def import_folder(self, folder):
        root = os.path.expanduser(self.s["games_root"])
        box = QMessageBox(self)
        box.setWindowTitle("Add a folder")
        box.setText(f"Add the game in {os.path.basename(folder.rstrip('/'))}?")
        box.setInformativeText(f"Copy or move it into your games folder ({root}), or use it where it is.")
        copy = box.addButton("Copy there", QMessageBox.ButtonRole.AcceptRole)
        move = box.addButton("Move there", QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton("Use where it is", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        clicked = box.clickedButton()
        name = guess_name(os.path.join(folder.rstrip("/"), "game.exe"))
        if clicked is keep:
            self.add_from_folder(folder, name)
            return
        if clicked not in (copy, move):
            return
        if os.path.realpath(folder).startswith(os.path.realpath(root) + "/"):
            self.add_from_folder(folder, name)
            return
        dest = unique_dir(root, os.path.basename(folder.rstrip("/")))

        def work(report):
            if clicked is move:
                try:
                    os.makedirs(root, exist_ok=True)
                    os.rename(folder, dest)  # instant on the same drive
                    return dest
                except OSError:
                    pass
            shutil.copytree(folder, dest, symlinks=True)
            if clicked is move:
                shutil.rmtree(folder)
            return dest

        def done(path, error):
            if error:
                QMessageBox.warning(self, "Couldn't copy it", str(error))
                return
            self.add_from_folder(path, name)

        self.run_task(f"{'Moving' if clicked is move else 'Copying'} {os.path.basename(folder)} to {dest}…", work, done)

    def add_from_folder(self, folder, name):
        programs = find_programs(folder)
        best = programs[0] if programs else ""
        if best and is_installer(best) and not any(not is_installer(p) for p in programs):
            r = QMessageBox.question(self, "Run the installer?",
                                     f"{name} comes as an installer ({os.path.basename(best)}).\n\nRun it in a new "
                                     "prefix now? When it finishes, Hangar finds the installed game and adds it.")
            if r == QMessageBox.StandardButton.Yes:
                self.run_installer(best, name)
                return
        preset = {"name": name}
        if best:
            preset.update(exe=best, runner=self.s["windows_runner"] if is_windows(best) else "native")
        else:
            self.say(f"Couldn't find a program in {folder}: pick it with Browse.", 12)
        d = EditDialog(self, kind=self.s["kind"], preset=preset, start_dir=folder)
        if d.exec():
            self.refresh()
            self.say(f"Added {d.e['name']}.")

    def run_installer(self, installer, name):
        e = dict(ENTRY_DEFAULTS, id=new_id(), name=name, kind=self.s["kind"], runner=self.s["windows_runner"],
                 added=int(time.time()))
        e["prefix"] = prefix_of(e, self.s)
        if os.path.exists(e["prefix"]):
            e["prefix"] = unique_dir(os.path.dirname(e["prefix"]), os.path.basename(e["prefix"]))
        started = time.time()
        try:
            p = start_process(e, target=installer, extra=[], log_name=f"{e['id']}-install")
        except (LaunchError, OSError) as ex:
            QMessageBox.warning(self, "Can't run the installer", str(ex))
            return
        self.say(f"Installing {name}: finish the installer, then Hangar asks what to add. "
                 "(A new prefix takes a minute to set up first.)", 30)
        timer = QTimer(self)

        def check():
            if p.poll() is None:
                return
            timer.stop()
            timer.deleteLater()
            drive = os.path.join(wine_prefix(e, self.s), "drive_c")
            programs = [x for x in find_programs(drive, newer_than=started - 5, max_depth=7) if not is_installer(x)]
            preset = dict(e, exe=programs[0] if programs else "")
            if not programs:
                self.say("The installer finished, but Hangar couldn't tell what it installed: pick the program "
                         "with Browse (it starts in the prefix's C: drive).", 15)
            d = EditDialog(self, kind=e["kind"], preset=preset, start_dir=drive)
            if d.exec():
                self.refresh()
                self.say(f"Added {d.e['name']}.")
            elif os.path.isdir(e["prefix"]) and QMessageBox.question(
                    self, "Delete the prefix?", f"Not added. Delete the prefix the installer used?\n{e['prefix']}") \
                    == QMessageBox.StandardButton.Yes:
                shutil.rmtree(e["prefix"], ignore_errors=True)

        timer.timeout.connect(check)
        timer.start(1000)

    def add_to_game_folder(self, eid, paths):
        e = get_entry(eid)
        if not e:
            return
        folder = game_folder(e)
        if not folder:
            return self.say(f"{e['name']} has no folder to put files in (it runs a command).")
        archives = [p for p in paths if os.path.isfile(p) and is_archive(p)]
        names = ", ".join(os.path.basename(p.rstrip("/")) for p in paths[:3]) + (" …" if len(paths) > 3 else "")
        box = QMessageBox(self)
        box.setWindowTitle(f"Add to {e['name']}")
        box.setText(f"Put {names} into {e['name']}'s folder?")
        box.setInformativeText(f"{folder}\n\nFiles with the same names there are replaced.")
        unpack = box.addButton("Unpack into it", QMessageBox.ButtonRole.AcceptRole) if archives else None
        copy = box.addButton("Copy as they are" if archives else "Copy", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(unpack or copy)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (unpack, copy) or clicked is None:
            return

        def done(_r, error):
            if error:
                QMessageBox.warning(self, "Couldn't add the files", str(error))
            else:
                self.say(f"Added {names} to {folder}")

        self.run_task(f"Adding files to {e['name']}…",
                      lambda report: copy_into(paths, folder, unpack=clicked is unpack, report=report), done)

    def edit(self, eid):
        e = get_entry(eid)
        if e and EditDialog(self, e).exec():
            self.refresh()

    def play(self, eid):
        e = get_entry(eid)
        if not e:
            return
        if self.launcher.is_running(eid):
            self.launcher.stop(e)
            self.say(f"Stopping {e['name']}…")
            return
        started_wivrn = e["vr"] and self.s["start_wivrn"] and start_wivrn()
        try:
            self.launcher.launch(e)
        except (LaunchError, OSError) as ex:
            QMessageBox.warning(self, f"Can't start {e['name']}", str(ex))
            return
        if started_wivrn:
            self.say(f"Started the WiVRn server for {e['name']}: connect your headset.", 15)
            return
        first = e["runner"] != "native" and not os.path.isdir(os.path.join(wine_prefix(e), "drive_c"))
        self.say(f"Starting {e['name']}…" + (" (the first start sets up its prefix, that takes a minute)"
                                            if first else ""), 12)

    def on_finished(self, eid, code, secs):
        e = get_entry(eid)
        if not e:
            return
        if code not in (0, -signal.SIGTERM, -signal.SIGKILL) and secs < 8:
            self.say(f"{e['name']} closed right away (exit code {code}). See ⋯ → Show the last log.", 15)
        else:
            self.say(f"{e['name']} closed after {human_time(secs)}.")

    def mark(self, eid, **changes):
        e = update_entry(eid, **changes)
        if e and "kind" in changes:
            refresh_shortcuts(e)
        self.refresh()

    def remove(self, eid):
        e = get_entry(eid)
        if not e:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Remove")
        box.setText(f"Remove {e['name']} from Hangar?")
        box.setInformativeText("The program's own files stay where they are.")
        cb = None
        pfx = prefix_of(e)
        if e["runner"] != "native" and os.path.isdir(pfx):
            cb = QCheckBox(f"Also delete its prefix (saves inside it are lost):\n{pfx}")
            box.setCheckBox(cb)
        box.setStandardButtons(QMessageBox.StandardButton.Cancel)
        rm = box.addButton("Remove", QMessageBox.ButtonRole.DestructiveRole)
        box.exec()
        if box.clickedButton() is not rm:
            return
        if self.launcher.is_running(eid):
            self.launcher.stop(e)
        remove_entry(eid)
        remove_shortcut(e)
        remove_shortcut(e, "desktop")
        remove_file(COVERS, e["cover"])
        remove_file(ICONS, e["icon"])
        if cb and cb.isChecked() and os.path.isdir(pfx) and pfx not in (HOME, "/") and \
                not any(x["id"] != eid and prefix_of(x) == pfx for x in library()):
            shutil.rmtree(pfx, ignore_errors=True)
        self.refresh()
        self.say(f"Removed {e['name']}.")

    def toggle_shortcut(self, eid, where="menu"):
        e = get_entry(eid)
        if not e:
            return
        place = "the app menu" if where == "menu" else "the desktop"
        if has_shortcut(e, where):
            remove_shortcut(e, where)
            self.say(f"Removed {e['name']} from {place}.")
            return
        try:
            make_shortcut(e, where)
        except OSError as ex:
            return self.say(f"Couldn't make the shortcut: {ex}")
        self.say(f"{e['name']} is on {place} now." if where == "desktop" else f"{e['name']} is in the app menu now.")

    def set_cover_file(self, eid, path):
        try:
            with open(path, "rb") as f:
                name = save_cover(eid, f.read())
        except (OSError, ValueError) as ex:
            return self.say(f"That image can't be used: {ex}")
        e = get_entry(eid)
        if e:
            remove_file(COVERS, e["cover"])
            update_entry(eid, cover=name)
        self.refresh()

    def run_tool(self, e, target, extra=None):
        try:
            start_process(e, target=target, extra=extra or [], tool=True, log_name=f"{e['id']}-tool")
            self.say(f"Starting {os.path.basename(target)}…")
        except (LaunchError, OSError) as ex:
            QMessageBox.warning(self, "Can't start it", str(ex))

    def run_in_prefix(self, e):
        path, _ = QFileDialog.getOpenFileName(self, f"Run in {e['name']}'s prefix",
                                              os.path.dirname(e["exe"]) if os.path.isfile(e["exe"]) else HOME,
                                              "Windows programs (*.exe *.msi *.bat *.lnk);;All files (*)")
        if path:
            self.run_tool(e, path)

    def winetricks(self, e):
        wt = which("winetricks")
        if not wt:
            return
        env, _server = wine_tool_env(e)
        if not os.path.isdir(os.path.join(env["WINEPREFIX"], "drive_c")):
            QMessageBox.information(self, "Prefix not set up yet",
                                    "Start the game once first (or open Wine settings), so its prefix exists.")
            return
        try:
            subprocess.Popen([wt, "--gui"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            self.say("Starting winetricks…")
        except OSError as ex:
            QMessageBox.warning(self, "Can't start winetricks", str(ex))

    def show_log(self, e):
        path = os.path.join(LOGS, f"{e['id']}.log")
        if not os.path.exists(path):
            return self.say("No log yet: it's written when the game starts.")
        d = QDialog(self)
        d.setWindowTitle(f"Log: {e['name']}")
        d.resize(860, 560)
        lay = QVBoxLayout(d)
        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        txt.setFont(QFont("monospace", 9))
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(max(0, os.path.getsize(path) - 400_000))
                txt.setPlainText(f.read())
        except OSError as ex:
            txt.setPlainText(str(ex))
        txt.moveCursor(txt.textCursor().MoveOperation.End)
        lay.addWidget(txt)
        row = QHBoxLayout()
        row.addWidget(label(path, "muted"), 1)
        row.addWidget(button("Open file", lambda: open_path(path)))
        row.addWidget(button("Close", d.accept, "primary"))
        lay.addLayout(row)
        d.exec()

    def open_settings(self):
        if SettingsDialog(self).exec():
            apply_theme(QApplication.instance())
            self.nsfw_shortcut.setKey(QKeySequence(settings()["nsfw_key"] or "Ctrl+Shift+N"))
        self.refresh()

    # drag and drop
    def dragEnterEvent(self, e):
        urls = [u for u in e.mimeData().urls() if u.isLocalFile() and os.path.exists(u.toLocalFile())]
        if urls and not urls[0].toLocalFile().lower().endswith(IMAGE_EXT):
            e.acceptProposedAction()
            self.overlay.setGeometry(self.centralWidget().rect().adjusted(10, 10, -10, -10))
            self.overlay.raise_()
            self.overlay.show()

    def dragLeaveEvent(self, _e):
        self.overlay.hide()

    def dropEvent(self, e):
        self.overlay.hide()
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile() and os.path.exists(u.toLocalFile())]
        e.acceptProposedAction()
        QTimer.singleShot(0, lambda: self.import_paths(paths))

    # ---- tray
    def tray_icon(self):
        if getattr(self, "tray", None) is None:
            icon = QApplication.instance().windowIcon()
            if icon.isNull():
                icon = QIcon(placeholder("Hangar", 64, 64))
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip("Hangar")
            menu = QMenu(self)
            menu.addAction("Open Hangar", self.show_window)
            menu.addSeparator()
            menu.addAction("Quit Hangar", self.quit_hangar)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self.tray_clicked)
        return self.tray

    def tray_clicked(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()

    def show_window(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def quit_hangar(self):
        self.quitting = True
        if self.close():
            QApplication.quit()
        else:
            self.quitting = False

    def hideEvent(self, e):
        # to the tray, minimised, another workspace's hide: NSFW locks again
        super().hideEvent(e)
        if not self.isVisible():
            QTimer.singleShot(0, self.lock_nsfw)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == e.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self.lock_nsfw)

    def closeEvent(self, e):
        if not getattr(self, "quitting", False) and settings()["close_to_tray"] \
                and QSystemTrayIcon.isSystemTrayAvailable():
            tray = self.tray_icon()
            tray.show()
            self.hide()
            e.ignore()
            if not getattr(self, "told_tray", False):
                self.told_tray = True
                tray.showMessage("Hangar is still running", "It's in the tray: click the icon to open it again, "
                                 "or right-click → Quit Hangar. (Settings can turn this off.)",
                                 QSystemTrayIcon.MessageIcon.Information, 5000)
            return
        if self.launcher.running:
            names = ", ".join(get_entry(i)["name"] for i in self.launcher.running if get_entry(i))
            r = QMessageBox.question(self, "Still running",
                                     f"{names} {'is' if len(self.launcher.running) == 1 else 'are'} still running. "
                                     "Close Hangar anyway? They keep running; playtime is counted up to now.")
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.launcher.save_running()
        if getattr(self, "tray", None) is not None:
            self.tray.hide()
        super().closeEvent(e)
        QApplication.quit()  # quitOnLastWindowClosed is off (the tray keeps it alive), so quit here


# ---- main --------------------------------------------------------------------------------------

class QuickRun(QDialog):
    """opening a program with Hangar (from the file manager, or `hangar FILE.exe`): its icon and name,
    what runs it, and Run. It can go into the library too; a program already there runs as set up"""

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.s = settings()
        self.e, self.known = quick_entry(path, self.s)
        self.setWindowTitle(f"Run {os.path.basename(path)}")
        self.setMinimumWidth(500)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 22, 22, 18)
        lay.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(16)
        pic = QLabel()
        pic.setFixedSize(72, 72)
        pm = QPixmap()
        data = exe_icon(path) if is_windows(path) else None
        if data and pm.loadFromData(data) and not pm.isNull():
            pic.setPixmap(rounded(pm, 72, 72, 14, fit=True))
        else:
            pic.setPixmap(placeholder(self.e["name"], 72, 72, r=14))
        top.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(4)
        self.name = QLineEdit(self.e["name"])
        self.name.setObjectName("title")
        self.name.setReadOnly(self.known)
        col.addWidget(self.name)
        where = label(path.replace(HOME, "~", 1), "muted", wrap=True)
        col.addWidget(where)
        if self.known:
            col.addWidget(label("In your library: runs with its own settings", "muted"))
        top.addLayout(col, 1)
        lay.addLayout(top)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.runner = QComboBox()
        if is_windows(path):
            for v in proton_versions():
                self.runner.addItem(f"Proton: {v['name']}", ("proton", v["path"]))
            for v in wine_versions():
                self.runner.addItem(f"Wine: {v['name']}", ("wine", v["path"]))
        self.runner.addItem("Native (run it as a Linux program)", ("native", ""))
        # what it's set to (its library entry), else the default Proton/Wine
        want = (self.e["runner"], self.e["version"] or (default_proton(self.s) if self.e["runner"] == "proton"
                                                         else default_wine(self.s) if self.e["runner"] == "wine" else ""))
        for i in range(self.runner.count()):
            r, v = self.runner.itemData(i)
            if r == want[0] and (not want[1] or v == want[1] or r == "native"):
                self.runner.setCurrentIndex(i)
                break
        self.runner.setEnabled(not self.known)
        form.addRow("Run with", self.runner)
        opts = QHBoxLayout()
        self.mangohud = QCheckBox("MangoHud")
        self.mangohud.setChecked(self.e["mangohud"])
        self.gamemode = QCheckBox("GameMode")
        self.gamemode.setChecked(self.e["gamemode"])
        for c in (self.mangohud, self.gamemode):
            c.setEnabled(not self.known)
            opts.addWidget(c)
        opts.addStretch(1)
        form.addRow("", opts)
        self.keep = QCheckBox("Add it to my library")
        self.keep.setVisible(not self.known)
        form.addRow("", self.keep)
        lay.addLayout(form)
        if is_windows(path) and is_installer(path) and not self.known:
            lay.addWidget(label("This looks like an installer: Install runs it in a new prefix and adds the game "
                                "it installs to your library.", "muted", wrap=True))

        lay.addStretch(1)
        row = QHBoxLayout()
        row.addWidget(button("Open folder", lambda: open_path(os.path.dirname(path))))
        row.addStretch(1)
        row.addWidget(button("Cancel", self.reject))
        if is_windows(path) and is_installer(path) and not self.known:
            row.addWidget(button("Install", self.install))
        go = button("Run", self.run, "primary")
        go.setDefault(True)
        row.addWidget(go)
        lay.addLayout(row)

    def showEvent(self, ev):
        # a small window of its own size: tiling compositors (niri) float fixed-size windows instead of
        # stretching them to the screen's height
        if not getattr(self, "_sized", False):
            self._sized = True
            self.adjustSize()
            self.setFixedSize(max(500, self.sizeHint().width()), self.sizeHint().height())
        super().showEvent(ev)

    def chosen(self):
        e = dict(self.e)
        if not self.known:
            r, v = self.runner.currentData()
            e.update(name=self.name.text().strip() or self.e["name"], runner=r, version=v,
                     mangohud=self.mangohud.isChecked(), gamemode=self.gamemode.isChecked())
        return e

    def run(self):
        e = self.chosen()
        try:
            if self.known or self.keep.isChecked():
                if not self.known:
                    e["id"] = new_id()
                    e["icon"] = save_exe_icon(e["id"], self.path) if is_windows(self.path) else ""
                    put_entry(e)
                # through --launch: it keeps the playtime, like a menu shortcut
                subprocess.Popen(self_command() + ["--launch", e["id"]], env=os.environ.copy(),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            else:
                start_process(e, self.s, log_name="quick")
        except (LaunchError, OSError) as ex:
            QMessageBox.warning(self, "Can't run it", str(ex))
            return
        self.accept()

    def install(self):
        """the installer goes through the main window, which finds the installed game and adds it"""
        self.accept()
        w = Window()
        w.show()
        QTimer.singleShot(200, lambda: w.run_installer(self.path, guess_name(self.path)))
        QuickRun.keep_alive = w


def cli_run(path):
    """`hangar --run FILE`: runs it right away (its library entry if it has one), no window"""
    if not os.path.isfile(path):
        print(f"hangar: no such file {path}", file=sys.stderr)
        return 1
    e, known = quick_entry(path)
    if known:
        return cli_launch(e["id"])
    try:
        p = start_process(e, log_name="quick")
    except (LaunchError, OSError) as ex:
        print(f"hangar: can't run {path}: {ex}", file=sys.stderr)
        subprocess.run(["notify-send", "-a", "Hangar", f"Can't run {os.path.basename(path)}", str(ex)], env=host_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return 1
    try:
        return p.wait()
    except KeyboardInterrupt:
        kill_group(p, signal.SIGTERM)
        return p.wait()


def cli_launch(eid):
    e = get_entry(eid)
    if not e:
        print(f"hangar: no entry {eid}", file=sys.stderr)
        return 1
    if e["vr"] and settings()["start_wivrn"]:
        start_wivrn()
    try:
        p = start_process(e)
    except (LaunchError, OSError) as ex:
        print(f"hangar: can't start {e['name']}: {ex}", file=sys.stderr)
        subprocess.run(["notify-send", "-a", "Hangar", f"Can't start {e['name']}", str(ex)], env=host_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return 1
    start = time.time()
    update_entry(eid, last_played=int(start))
    try:
        code = p.wait()
    except KeyboardInterrupt:
        kill_group(p, signal.SIGTERM)
        code = p.wait()
    add_playtime(eid, time.time() - start)
    return code


def cli_check():
    """prints what Hangar finds on this PC, and whether downloads work"""
    print(f"Hangar {VERSION}")
    pv = proton_versions()
    print(f"Proton ({len(pv)}): " + (", ".join(v["name"] for v in pv) or "none") + f"\n  default: {default_proton() or '-'}")
    wv = wine_versions()
    print(f"Wine ({len(wv)}): " + (", ".join(v["name"] for v in wv) or "none"))
    print(f"VR: WiVRn {'running' if wivrn_running() else ('installed' if which('wivrn-server') else 'not installed')}"
          f", xrizer {xrizer_dir() or 'not found'}")
    for tool in ("umu-run", "winetricks", "mangohud", "gamemoderun", "gamescope"):
        print(f"{tool}: {which(tool) or 'not found'}")
    try:
        http_get("https://api.github.com/", timeout=10)
        print("downloads: ok")
    except Exception as ex:
        print(f"downloads: FAILED ({ex})")
        return 1
    return 0


INSTANCE = None
INSTANCE_SOCKET = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/hangar-{os.getuid()}", "hangar.sock")


def hand_over(paths):
    """True when another Hangar is running: it was told to show itself (and to import `paths`)"""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(1)
            c.connect(INSTANCE_SOCKET)
            c.sendall((json.dumps({"show": True, "paths": paths}) + "\n").encode())
        return True
    except OSError:
        return False


class InstanceServer(QObject):
    """a Unix socket other Hangar starts talk to (Qt's QLocalServer would pull QtNetwork into the AppImage)"""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        os.makedirs(os.path.dirname(INSTANCE_SOCKET), exist_ok=True)
        try:
            os.remove(INSTANCE_SOCKET)  # nobody answered on it (hand_over failed), so it's left from a crash
        except OSError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(INSTANCE_SOCKET)
        os.chmod(INSTANCE_SOCKET, 0o600)
        self.sock.listen(4)
        self.sock.setblocking(False)
        self.notifier = QSocketNotifier(self.sock.fileno(), QSocketNotifier.Type.Read, self)
        self.notifier.activated.connect(self.incoming)

    def incoming(self, *_):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        data = b""
        try:
            conn.settimeout(1)
            while not data.endswith(b"\n") and len(data) < 1 << 20:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        finally:
            conn.close()
        try:
            msg = json.loads(data.decode() or "{}")
        except ValueError:
            msg = {}
        self.win.show_window()
        paths = [p for p in msg.get("paths") or [] if isinstance(p, str) and os.path.exists(p)]
        if paths:
            QTimer.singleShot(100, lambda: self.win.import_paths(paths))

    def close(self):
        self.notifier.setEnabled(False)
        self.sock.close()
        try:
            os.remove(INSTANCE_SOCKET)
        except OSError:
            pass


def listen(win):
    global INSTANCE
    try:
        INSTANCE = InstanceServer(win)
    except OSError:
        INSTANCE = None


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--check":
        sys.exit(cli_check())
    if len(sys.argv) >= 3 and sys.argv[1] == "--launch":
        sys.exit(cli_launch(sys.argv[2]))
    if len(sys.argv) >= 3 and sys.argv[1] == "--run":
        sys.exit(cli_run(os.path.abspath(os.path.expanduser(sys.argv[2]))))
    if len(sys.argv) >= 2 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    if len(sys.argv) >= 2 and sys.argv[1] in ("-v", "--version"):
        print(f"Hangar {VERSION}")
        return
    app = QApplication(sys.argv)
    app.setApplicationName("hangar")
    app.setDesktopFileName("hangar")
    if os.path.exists(ICON):
        app.setWindowIcon(QIcon(ICON))
    apply_theme(app)
    # keep the file manager's "open with" pointing at this copy (an AppImage that moved, a new version)
    if settings()["exe_opener"]:
        try:
            register_opener(True)
        except OSError:
            pass
    args = [a for a in sys.argv[1:] if a != "--open"]
    paths = [os.path.abspath(a) for a in args if os.path.exists(a)]
    # one program opened from the file manager (or `hangar FILE.exe`): the small run window, PortProton-style
    if len(paths) == 1 and os.path.isfile(paths[0]) and not is_archive(paths[0]) \
            and (is_windows(paths[0]) or "--open" in sys.argv):
        d = QuickRun(paths[0])
        d.show()
        app.setQuitOnLastWindowClosed(True)
        sys.exit(app.exec())
    # one Hangar at a time: starting it again (menu, dock, file manager) brings back the running one,
    # which may be hidden in the tray, and hands it the files
    if hand_over(paths):
        return
    app.setQuitOnLastWindowClosed(False)
    w = Window()
    listen(w)
    app.aboutToQuit.connect(lambda: INSTANCE and INSTANCE.close())
    w.show()
    if paths:
        QTimer.singleShot(200, lambda: w.import_paths(paths))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
