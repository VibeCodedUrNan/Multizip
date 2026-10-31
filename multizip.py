"""
MultiZip - a 7-Zip-style archive manager for Windows.

GUI  : Home screen with drag & drop, file-manager window, custom themes and
       background images (View > Settings) (browse folders, open archives like folders,
       Add / Extract / Test / Delete, multi-archive extraction).
CLI  : multizip.exe x|xh|xd|a|t <files...>   (used by the right-click menu)
         x  = extract to folder named after each archive
         xh = extract here
         xd = extract dialog
         a  = add to archive dialog
         t  = test archives
       multizip.exe <archive|folder>          open in the manager
"""
import os
import queue
import string
import sys
import tarfile
import tempfile
import threading
import zipfile
import bz2
import copy
import email.message
import fnmatch
import glob
import hashlib
import heapq
import http.client
import json
import socket
import ssl
import time
import traceback
import types
import urllib.error
import urllib.parse
import urllib.request
import uuid
import re
import subprocess
import gzip
import lzma
import shutil
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

try:
    import py7zr
    # before 0.20.2 py7zr can write outside the target folder: such a copy is ignored and 7-Zip handles 7z instead
    if tuple(int(x) for x in re.findall(r"\d+", py7zr.__version__)[:3]) < (0, 20, 2):
        py7zr = None
except ImportError:
    py7zr = None
try:
    import rarfile
except ImportError:
    rarfile = None
try:
    import PIL
    from PIL import Image, ImageFont, ImageOps
    PIL_VER = getattr(PIL, "__version__", "")
except ImportError:
    Image = ImageFont = ImageOps = None
    PIL_VER = ""
try:
    from PIL import ImageTk
except Exception:
    ImageTk = None
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    DND = True
except ImportError:
    TkinterDnD = DND_FILES = None
    DND = False
BaseTk = TkinterDnD.Tk if DND else tk.Tk



def _setup_rar_tools():
    """Point rarfile at a bundled / installed UnRAR.exe or 7z.exe."""
    if not rarfile:
        return
    here = [os.path.dirname(os.path.abspath(sys.executable)), getattr(sys, "_MEIPASS", ""),
            os.path.dirname(os.path.abspath(__file__))]
    pf = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    found = False
    for d in here + [os.path.join(p, "WinRAR") for p in pf if p]:
        f = os.path.join(d, "UnRAR.exe")
        if os.path.isfile(f):
            rarfile.UNRAR_TOOL, found = f, True
            break
    if not found:
        for d in here + [os.path.join(p, "7-Zip") for p in pf if p]:
            f = os.path.join(d, "7z.exe")
            if os.path.isfile(f):
                rarfile.SEVENZIP_TOOL = f
                break
    try:
        rarfile.tool_setup(force=True)
    except Exception:
        pass


_setup_rar_tools()

APP = "MultiZip"
TAR = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
ZST_TAR = (".tar.zst", ".tzst")
try:
    from compression import zstd as _zstd          # Python 3.14+
    ZSTD = "std"
except Exception:
    try:
        import zstandard as _zstd                  # pip install zstandard
        ZSTD = "pkg"
    except Exception:
        _zstd, ZSTD = None, None


def zstd_reader(path, mode="rb"):
    if ZSTD == "std":
        return _zstd.open(path, "rb")
    f = open(path, "rb")
    d = _zstd.ZstdDecompressor()
    try:
        return d.stream_reader(f, read_across_frames=True, closefd=True)
    except TypeError:
        return d.stream_reader(f)


class _PkgZstdWriter:
    def __init__(self, path, level):
        self.f = open(path, "wb")
        self.cm = _zstd.ZstdCompressor(level=level).stream_writer(self.f)
        self.w = self.cm.__enter__()

    def write(self, b):
        return self.w.write(b)

    def close(self):
        self.cm.__exit__(None, None, None)           # ends the zstd frame
        self.f.close()


def zstd_writer(path, level):
    lv = {0: 1, 1: 3, 6: 10, 9: 19}.get(level, 10)
    return _zstd.open(path, "wb", level=lv) if ZSTD == "std" else _PkgZstdWriter(path, lv)


SINGLE = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open, ".lzma": lzma.open, ".zst": zstd_reader}
# formats handled by the 7-Zip engine: a double-click opens these as archives
ENGINE_EXTS = (".iso", ".cab", ".wim", ".swm", ".esd", ".vhd", ".vhdx", ".vmdk", ".vdi", ".qcow2", ".dmg",
               ".arj", ".lzh", ".lha", ".z", ".taz", ".tlz", ".cpio", ".rpm", ".deb", ".xar", ".squashfs",
               ".sqfs", ".cramfs", ".chm", ".udf", ".hfs", ".apfs", ".ntfs", ".fat", ".mbr", ".gpt")
ALL = (".zip", ".7z", ".rar") + TAR + ZST_TAR + tuple(SINGLE) + ENGINE_EXTS
RAR_HELP = ("RAR needs the 7-Zip engine.\nOpen Help > Supported formats and click 'Get 7-Zip engine' "
            "(free, about 2 MB),\nor install 7-Zip / WinRAR and restart MultiZip.")


class Cancelled(Exception):
    pass


# ===================================================== universal engine (7-Zip)
# 7-Zip reads ~70 formats (ISO, CAB, WIM, VHD, DMG, ARJ, LZH, RPM, DEB, RAR, 7z ...) and can write
# encrypted 7z/zip. MultiZip drives its command-line program when it is available.
ENGINE_HELP = ("This format needs the 7-Zip engine.\nOpen Help > Supported formats and click "
               "'Get 7-Zip engine' (free, about 2 MB),\nor install 7-Zip from https://www.7-zip.org "
               "and restart MultiZip.")
_ENGINE = {"done": False, "path": None, "ver": None}
FORCED = {}                     # path -> kind chosen with "Open as archive"


def engine_dir():
    return os.path.join(os.path.dirname(settings_path()), "engine")


def _flags():
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return dict(startupinfo=si, creationflags=0x08000000)       # no console window


def _probe(exe):
    """Run the program once; return its (major, minor) version if it really is 7-Zip."""
    try:
        r = subprocess.run([exe], stdin=subprocess.DEVNULL, capture_output=True, timeout=15, **_flags())
        m = re.search(r"(?:7-Zip|p7zip Version)[^\d]*(\d+)\.(\d+)", r.stdout.decode("utf-8", "replace"))
        return (int(m.group(1)), int(m.group(2))) if m else None
    except Exception:
        return None


def _engine_candidates():
    names = ("7z.exe",) if os.name == "nt" else ("7zz", "7z", "7zr")
    dirs = [getattr(sys, "_MEIPASS", ""), os.path.dirname(os.path.abspath(sys.executable)),
            os.path.dirname(os.path.abspath(__file__)), engine_dir()]
    for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        v = os.environ.get(k)
        if v:
            dirs.append(os.path.join(v, "7-Zip"))
    out = []
    for d in dirs:
        for n in names:
            f = os.path.join(d, n) if d else ""
            if f and os.path.isfile(f) and (os.name != "nt" or os.path.isfile(os.path.join(d, "7z.dll"))):
                if os.name != "nt" and d == getattr(sys, "_MEIPASS", None) and not os.access(f, os.X_OK):
                    try:                                  # the copy bundled by build.sh may unpack without its x bit
                        os.chmod(f, os.stat(f).st_mode | 0o111)
                    except OSError:
                        pass
                out.append(f)
    out += [w for w in (shutil.which(n) for n in names) if w]
    return list(dict.fromkeys(out))


def engine_path(refresh=False):
    if refresh or not _ENGINE["done"]:
        _ENGINE.update(done=True, path=None, ver=None)
        for c in _engine_candidates():
            v = _probe(c)
            if v:
                _ENGINE.update(path=c, ver=v)
                break
    return _ENGINE["path"]


def _modern():
    return (_ENGINE["ver"] or (0, 0)) >= (16, 0)


def _enc():
    return "utf-8" if _modern() or os.name != "nt" else "oem"


def _run(args, cancelled=lambda: False, on_line=None, cwd=None):
    """Run the engine; returns (exit code, all output). Honours Cancel."""
    exe = engine_path()
    if not exe:
        raise RuntimeError(ENGINE_HELP)
    proc = subprocess.Popen([exe] + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, cwd=cwd, **_flags())
    q, lines = queue.Queue(), []

    def reader():
        for raw in iter(proc.stdout.readline, b""):
            q.put(raw)
        q.put(None)
    threading.Thread(target=reader, daemon=True).start()
    while True:
        if cancelled():
            proc.kill()
            proc.wait()
            raise Cancelled
        try:
            raw = q.get(timeout=0.1)
        except queue.Empty:
            continue
        if raw is None:
            break
        line = raw.decode(_enc(), "replace").rstrip("\r\n")
        lines.append(line)
        if on_line:
            on_line(line)
    return proc.wait(), "\n".join(lines)


def _pw(pwd):
    return ["-p" + pwd] if pwd else []


def engine_error(out, rc, had_pwd):
    low = out.lower()
    if any(t in low for t in ("wrong password", "enter password", "encrypted archive", "password is incorrect")):
        return RuntimeError("Wrong password." if had_pwd else "Password required - this archive is encrypted.")
    errs = [l.strip() for l in out.splitlines()
            if re.match(r"\s*(ERROR|Can not|Cannot|Unsupported|Headers Error|Data Error)", l, re.I)]
    return RuntimeError(" ".join(errs[-3:]) or out.strip()[-300:] or f"7-Zip stopped (code {rc}).")


def parse_slt(text):
    """Parse `7z l -slt` output -> [(name, size, packed, mtime, isdir)]."""
    if "----------" in text:
        text = text.split("----------", 1)[1]
    out = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        f = {}
        for line in block.splitlines():
            k, sep, v = line.partition(" = ")
            if sep:
                f[k.strip()] = v.strip()
        if "Path" not in f:
            continue
        isdir = f.get("Folder") == "+" or f.get("Attributes", "").startswith("D")
        num = lambda key: int(f[key]) if f.get(key, "").isdigit() else None
        try:
            mt = datetime.strptime(f.get("Modified", "")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            mt = None
        out.append((norm(f["Path"]), num("Size"), num("Packed Size"), mt, isdir))
    return out


def engine_list(p, pwd=None):
    rc, out = _run(["l", "-slt", "-y"] + (["-sccUTF-8"] if _modern() else []) + _pw(pwd) + ["--", p])
    entries = parse_slt(out) if rc < 2 else []
    if rc >= 2 or (rc and not entries):
        raise engine_error(out, rc, bool(pwd))
    return entries


def _out_dir(dest):
    d = os.path.normpath(dest)
    return d + "." if d.endswith(("\\", "/")) else d


def engine_extract(p, dest, pwd, members, tick, cancelled, keep=False):
    w = wanted(members)
    chosen = [e[0] for e in engine_list(p, pwd) if w(e[0])]
    for n in chosen:
        safe(dest, n)                                      # same zip-slip guard as the built-in readers
    if not chosen:
        return
    os.makedirs(dest, exist_ok=True)
    args = ["x", "-y", "-aos" if keep else "-aoa", "-o" + _out_dir(dest)]       # skip / overwrite existing files
    args += (["-bb1", "-bsp0", "-sccUTF-8"] if _modern() else []) + _pw(pwd)
    lst = None
    if members is not None:
        fd, lst = tempfile.mkstemp(suffix=".txt", prefix="multizip_list_")
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(chosen))
        args.append("-i@" + lst)                           # a switch, so it still works before "--"
    args += ["--", p]                                      # "--": a name starting with "-" is never read as an option
    done = [0]

    def on_line(line):
        if done[0] < len(chosen) and re.match(r"(- |Extracting\s)", line):
            done[0] += 1
            tick(1)
    try:
        rc, out = _run(args, cancelled, on_line)
    finally:
        if lst:
            try:
                os.remove(lst)
            except OSError:
                pass
    if rc >= 2:
        raise engine_error(out, rc, bool(pwd))
    if done[0] < len(chosen):
        tick(len(chosen) - done[0])


def engine_test(p, pwd, tick, cancelled):
    n = len(engine_list(p, pwd))
    rc, out = _run(["t", "-y"] + (["-bsp0", "-sccUTF-8"] if _modern() else []) + _pw(pwd) + ["--", p], cancelled)
    if rc != 0:
        raise engine_error(out, rc, bool(pwd))
    tick(max(1, n))


def engine_create(out, paths, fmt, level, pwd, tick, cancelled):
    """7z or zip through 7-Zip (zip gets AES-256 encryption when a password is set)."""
    if os.path.exists(out):
        os.remove(out)
    mx = min((0, 1, 3, 5, 7, 9), key=lambda v: abs(v - level))
    args = ["a", "-t" + fmt, f"-mx={mx}", "-y"] + (["-bso0", "-bsp0"] if _modern() else [])
    if pwd:
        args += _pw(pwd) + (["-mhe=on"] if fmt == "7z" else ["-mem=AES256"])
    for p in paths:
        if cancelled():
            raise Cancelled
        p = os.path.abspath(p)
        rc, txt = _run(args + ["--", os.path.abspath(out), os.path.basename(p)], cancelled, cwd=os.path.dirname(p))
        if rc >= 2:
            raise engine_error(txt, rc, False)
        tick(1, os.path.basename(p))


def verify_signature(path, who="Pavlov"):
    """Windows: the file must carry a valid digital signature from `who`, or this raises RuntimeError."""
    script = (f"$s = Get-AuthenticodeSignature -LiteralPath {ps_quote(path)}; $n = ''; "
              "if ($s.SignerCertificate) { $n = $s.SignerCertificate.Subject }; "
              "[pscustomobject]@{ Status = [string]$s.Status; Subject = $n } | ConvertTo-Json -Compress")
    info = None
    try:
        rc, out, _err = ps_run(script, 90)
        if rc == 0 and out.strip():
            info = json.loads(out.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if not isinstance(info, dict):
        raise RuntimeError("Could not check the 7-Zip download's digital signature, so it was not used.\n"
                           "Install 7-Zip from https://www.7-zip.org instead.")
    if info.get("Status") != "Valid" or who.lower() not in str(info.get("Subject", "")).lower():
        raise RuntimeError(f"The downloaded 7-Zip is not validly signed by its author (signature: "
                           f"{info.get('Status')}), so it was not used.\nInstall 7-Zip from https://www.7-zip.org instead.")


def download_engine(tick, cancelled, dest=None):
    """Download 7-Zip from 7-zip.org and unpack 7z.exe + 7z.dll (no admin rights needed)."""
    import platform
    if os.name != "nt":
        raise RuntimeError("Install 7-Zip with your package manager (for example: sudo apt install 7zip).")
    dest = dest or engine_dir()
    os.makedirs(dest, exist_ok=True)
    x64 = platform.machine().lower() in ("amd64", "x86_64", "arm64", "aarch64")
    base = "https://www.7-zip.org/"
    req = lambda u: urllib.request.Request(u, headers={"User-Agent": "MultiZip"})
    tick(0, "Finding the latest 7-Zip ...")
    names = []
    try:
        html = urllib.request.urlopen(req(base + "download.html"), timeout=30).read().decode("latin-1")
        names = re.findall(r'href="a/(7z\d+-x64\.msi)"' if x64 else r'href="a/(7z\d+\.msi)"', html, re.I)
    except Exception:
        pass
    names = list(dict.fromkeys(names)) or ["7z2501-x64.msi" if x64 else "7z2501.msi"]
    tmp = tempfile.mkdtemp(prefix="multizip_engine_")
    try:
        msi, err = os.path.join(tmp, "7zip.msi"), None
        tick(0, "Downloading 7-Zip ...")
        for n in names[:2]:
            try:
                with urllib.request.urlopen(req(base + "a/" + n), timeout=90) as r, open(msi, "wb") as f:
                    shutil.copyfileobj(r, f)
                err = None
                break
            except Exception as e:
                err = e
        if err:
            raise RuntimeError(f"Could not download 7-Zip ({err}).\nInstall it from https://www.7-zip.org "
                               "and restart MultiZip.")
        if cancelled():
            raise Cancelled
        tick(1, "Checking the digital signature ...")
        verify_signature(msi)
        tick(1, "Unpacking ...")
        out = os.path.join(tmp, "x")
        r = subprocess.run(f'msiexec /a "{msi}" /qn TARGETDIR="{out}"', capture_output=True, **_flags())
        exe = dll = None
        for root_, _d, files in os.walk(out):
            if "7z.exe" in files:
                exe = os.path.join(root_, "7z.exe")
            if "7z.dll" in files:
                dll = os.path.join(root_, "7z.dll")
        if not (exe and dll):
            raise RuntimeError(f"Could not unpack the 7-Zip installer (msiexec code {r.returncode}).\n"
                               "Install 7-Zip from https://www.7-zip.org instead.")
        shutil.copy2(exe, dest)
        shutil.copy2(dll, dest)
        tick(1, "Checking ...")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not _probe(os.path.join(dest, "7z.exe")):
        raise RuntimeError("The downloaded 7-Zip would not start. Install 7-Zip from https://www.7-zip.org.")
    engine_path(refresh=True)


_ZCACHE = {}


def zst_tar(p):
    """A .tar.zst is unpacked once to a temporary .tar that the tar reader can seek in."""
    key = (os.path.abspath(p), os.path.getmtime(p))
    if key not in _ZCACHE or not os.path.exists(_ZCACHE[key]):
        fd, tmp = tempfile.mkstemp(suffix=".tar", prefix="multizip_zst_")
        with zstd_reader(p) as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
        _ZCACHE[key] = tmp
    return _ZCACHE[key]


def _clean_zcache():
    for f in _ZCACHE.values():
        try:
            os.remove(f)
        except OSError:
            pass


import atexit
atexit.register(_clean_zcache)


def tar_source(p):
    return zst_tar(p) if p.lower().endswith(ZST_TAR) else p


def sniff_kind(p):
    """Guess how to open a file with an unknown extension by looking at its first bytes."""
    try:
        with open(p, "rb") as f:
            h = f.read(512)
    except OSError:
        return "engine"
    if h[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if h[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if h[:4] == b"Rar!":
        return "rar"
    if h[257:262] == b"ustar":
        return "tar"
    return "engine"


# ================================================================ backend
def kind(p):
    l = p.lower()
    if FORCED:
        f = FORCED.get(os.path.abspath(p))
        if f:
            return f
    if l.endswith(".zip"):
        return "zip"
    if l.endswith(ZST_TAR):
        return "tar" if ZSTD else None
    if l.endswith(TAR):
        return "tar"
    if l.endswith(".7z"):
        return "7z"
    if l.endswith(".rar"):
        return "rar"
    if l.endswith(".zst"):
        return "single" if ZSTD else None
    if l.endswith(tuple(SINGLE)):
        return "single"
    if l.endswith(ENGINE_EXTS):
        return "engine" if engine_path() else None
    return None


def backend(p):
    """Which reader will really be used for this archive."""
    k = kind(p)
    if k == "7z":
        return "py7zr" if py7zr else ("engine" if engine_path() else None)
    if k == "rar":
        return "engine" if engine_path() else ("rarfile" if rarfile else None)
    return k


def stem(p):
    n, l = os.path.basename(p), p.lower()
    for e in sorted(ALL, key=len, reverse=True):
        if l.endswith(e):
            return n[: -len(e)] or n
    return os.path.splitext(n)[0] or n


def norm(n):
    n = n.replace("\\", "/")
    while n.startswith("./"):
        n = n[2:]
    return n.strip("/")


def human(n):
    if n is None:
        return ""
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def safe(dest, member):
    base = os.path.realpath(dest)
    t = os.path.realpath(os.path.join(base, member))
    if t != base and not t.startswith(base + os.sep):
        raise ValueError(f"Unsafe path in archive: {member}")


_BAD_NAME = re.compile(r'[<>:"|?*\x00-\x1f]')


def extracted_path(root, member):
    """Where archive member `member` landed under `root`, or None. Both readers store characters Windows
    forbids as '_'; a trailing dot is dropped by zipfile but turned into '_' by 7-Zip."""
    parts = [s for s in member.split("/") if s]
    for fix in (lambda s: s, lambda s: _BAD_NAME.sub("_", s).rstrip("."),
                lambda s: re.sub(r"\.$", "_", _BAD_NAME.sub("_", s))):
        p = os.path.join(root, *map(fix, parts))
        if os.path.exists(p):
            return p
    return None


def _pw_problem(e):
    t = str(e).lower()
    return "password" in t or "encrypted" in t


def _no_backend(p):
    k = kind(p)
    return RuntimeError(ENGINE_HELP if k in ("7z", "rar", "engine") else "Format not supported.")


def list_archive(p, pwd=None):
    """-> [(name, size, packed, mtime, isdir)]"""
    b = backend(p)
    if b is None:
        raise _no_backend(p)
    if b == "engine":
        return engine_list(p, pwd)
    try:
        return _list_native(p, b, pwd)
    except Exception as e:
        if engine_path() and not _pw_problem(e):             # built-in reader can't cope -> try 7-Zip
            try:
                return engine_list(p, pwd)
            except Exception:
                pass
        raise


def _list_native(p, k, pwd=None):
    out = []
    if k == "zip":
        with zipfile.ZipFile(p) as z:
            for i in z.infolist():
                try:
                    mt = datetime(*i.date_time)
                except ValueError:
                    mt = None
                out.append((norm(i.filename), i.file_size, i.compress_size, mt, i.is_dir()))
    elif k == "tar":
        with tarfile.open(tar_source(p)) as t:
            for m in t.getmembers():
                try:
                    mt = datetime.fromtimestamp(m.mtime)
                except (OverflowError, OSError, ValueError):
                    mt = None
                out.append((norm(m.name), m.size, None, mt, m.isdir()))
    elif k == "py7zr":
        with py7zr.SevenZipFile(p, password=pwd) as z:
            for f in z.list():
                out.append((norm(f.filename), f.uncompressed, getattr(f, "compressed", None),
                            getattr(f, "creationtime", None), f.is_directory))
    elif k == "rarfile":
        with rar_open(p, pwd) as r:
            for i in r.infolist():
                try:
                    mt = datetime(*i.date_time)
                except ValueError:
                    mt = None
                out.append((norm(i.filename), i.file_size, i.compress_size, mt, i.is_dir()))
    elif k == "single":
        out.append((stem(p), None, os.path.getsize(p),
                    datetime.fromtimestamp(os.path.getmtime(p)), False))
    else:
        raise RuntimeError("Format not supported (install py7zr / rarfile)")
    return out


def rar_open(p, pwd=None):
    try:
        r = rarfile.RarFile(p)
    except rarfile.RarCannotExec:
        raise RuntimeError(RAR_HELP)
    if pwd:
        r.setpassword(pwd)
    return r


def wanted(members):
    return lambda n: members is None or any(n == m or n.startswith(m + "/") for m in members)


# ------------------------------------------------ safety: web marks and disk space
MOTW = os.name == "nt"                       # the marks live in NTFS streams, so only Windows has them
LOW_DISK = 64 * 1024 * 1024                  # never fill a disk below this
BOMB_BYTES, BOMB_RATIO = 20 * 1024 ** 3, 100


class DiskFull(RuntimeError):
    pass


def zone_path(p):
    return p + ":Zone.Identifier"


def mark_web(p, url=""):
    """Add the 'Mark of the Web' that browsers add, so Windows warns before a downloaded program runs."""
    if not MOTW:
        return
    url = re.sub(r"//[^/@\s]*@", "//", re.sub(r"[\r\n]", "", url or ""))        # no passwords, no line breaks
    text = "[ZoneTransfer]\r\nZoneId=3\r\n" + (f"HostUrl={url}\r\n" if url else "")
    try:
        with open(zone_path(p), "wb") as f:
            f.write(text.encode("utf-8"))
    except OSError:
        pass


def from_web(p):
    """True if the file carries the mark for the internet (or a worse) zone."""
    if not MOTW:
        return False
    try:
        with open(zone_path(p), "rb") as f:
            raw = f.read(4096)
    except OSError:
        return False
    m = re.search(r"ZoneId\s*=\s*(\d+)", raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8",
                                                     "ignore"))
    return bool(m) and int(m.group(1)) >= 3


def mark_extracted(p, dest, pwd, members, kept=()):
    """Files unpacked from an archive that came from the internet carry the same mark (not the `kept` own files)."""
    try:
        w = wanted(members)
        for name, _size, _packed, _mtime, isdir in list_archive(p, pwd):
            if not isdir and w(name) and name not in kept:
                safe(dest, name)
                f = os.path.join(dest, name)
                if os.path.isfile(f) and not os.path.islink(f):
                    mark_web(f)
    except Exception:
        pass


def unpacked_size(p, members=None, pwd=None):
    """(items, bytes once unpacked) from the archive's own listing, or None if it cannot be read."""
    try:
        w = wanted(members)
        ents = [e for e in list_archive(p, pwd) if w(e[0])]
    except Exception:
        return None
    return max(1, len(ents)), sum(e[1] or 0 for e in ents if not e[4])


def existing_targets(p, dest, pwd=None, members=None):
    """The archive's files that already exist at `dest`, i.e. what extracting there would overwrite."""
    try:
        w = wanted(members)
        return [n for n, _s, _p, _m, isdir in list_archive(p, pwd)
                if not isdir and w(n) and os.path.isfile(os.path.join(dest, *n.split("/")))]
    except Exception:
        return []


def free_space_at(path):
    """(an existing folder on the same disk as `path`, free bytes there)"""
    d = os.path.abspath(path)
    while not os.path.isdir(d):
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return d, shutil.disk_usage(d).free


def space_problem(sizes, dests):
    """A message if the unpacked files (sizes[i] going to dests[i]) would not fit, else None."""
    need = {}
    for size, dest in zip(sizes, dests):
        if not size:
            continue
        try:
            d, free = free_space_at(dest)
            key = os.path.splitdrive(d)[0].lower() or os.stat(d).st_dev
        except OSError:
            continue
        need.setdefault(key, [d, free, 0])[2] += size
    for d, free, total in need.values():
        if total + LOW_DISK > free:
            return (f"There is not enough free space on {d}\n\n"
                    f"The files need about {human(total)} but only {human(free)} is free.")
    return None


def check_room(dest):
    """Stop extracting before the disk is completely full."""
    try:
        if shutil.disk_usage(dest).free < LOW_DISK:
            raise DiskFull("The disk is almost full, so extraction was stopped.")
    except OSError:
        pass


def extract(p, dest, pwd, members, tick, cancelled, mark=True, keep=False):
    """keep=True leaves files that already exist at `dest` alone instead of overwriting them."""
    web = mark and from_web(p)
    kept = set(existing_targets(p, dest, pwd, members)) if web and keep else set()
    _extract_any(p, dest, pwd, members, tick, cancelled, keep)
    if web:
        mark_extracted(p, dest, pwd, members, kept)


def _extract_any(p, dest, pwd, members, tick, cancelled, keep=False):
    b = backend(p)
    if b is None:
        raise _no_backend(p)
    if b == "engine":
        return engine_extract(p, dest, pwd, members, tick, cancelled, keep)
    try:
        return _extract_native(p, b, dest, pwd, members, tick, cancelled, keep)
    except (Cancelled, DiskFull):
        raise
    except Exception as e:
        if engine_path() and not _pw_problem(e) and "Unsafe path" not in str(e):
            try:                                              # built-in reader can't cope -> try 7-Zip
                return engine_extract(p, dest, pwd, members, lambda n=1, label=None: None, cancelled, keep)
            except Cancelled:
                raise
            except Exception:
                pass
        raise


def _extract_native(p, k, dest, pwd, members, tick, cancelled, keep=False):
    w = wanted(members)
    pb = pwd.encode() if pwd else None
    os.makedirs(dest, exist_ok=True)
    there = lambda name: keep and os.path.isfile(os.path.join(dest, *norm(name).split("/")))   # a file to keep
    n = 0
    if k == "zip":
        with zipfile.ZipFile(p) as z:
            for i in z.infolist():
                if cancelled():
                    raise Cancelled
                if w(norm(i.filename)):
                    safe(dest, i.filename)
                    if there(i.filename):
                        tick(1)
                        continue
                    n += 1
                    if n % 16 == 1:
                        check_room(dest)
                    z.extract(i, dest, pwd=pb)
                    tick(1)
    elif k == "tar":
        with tarfile.open(tar_source(p)) as t:
            for m in t.getmembers():
                if cancelled():
                    raise Cancelled
                if w(norm(m.name)):
                    safe(dest, m.name)
                    if there(m.name):
                        tick(1)
                        continue
                    n += 1
                    if n % 16 == 1:
                        check_room(dest)
                    if hasattr(tarfile, "data_filter"):
                        t.extract(m, dest, filter="data")
                    else:
                        t.extract(m, dest)
                    tick(1)
    elif k == "py7zr":
        with py7zr.SevenZipFile(p, password=pwd or None) as z:
            names = [n for n in z.getnames() if w(norm(n))]
            for n in names:
                safe(dest, n)
            if keep:
                names = [n for n in names if not there(n)]
            if members is None and not keep:
                z.extractall(path=dest)
            elif names:
                z.extract(path=dest, targets=names)
        tick(max(1, len(names)))
    elif k == "rarfile":
        with rar_open(p, pwd) as r:
            for i in r.infolist():
                if cancelled():
                    raise Cancelled
                if w(norm(i.filename)):
                    safe(dest, i.filename)
                    if there(i.filename):
                        tick(1)
                        continue
                    n += 1
                    if n % 16 == 1:
                        check_room(dest)
                    r.extract(i, dest, pwd=pwd or None)
                    tick(1)
    elif k == "single":
        if there(stem(p)):
            return tick(1)
        op = SINGLE["." + p.lower().rsplit(".", 1)[1]]
        with op(p, "rb") as s, open(os.path.join(dest, stem(p)), "wb") as d:
            while True:
                if cancelled():
                    raise Cancelled
                chunk = s.read(1024 * 1024)
                if not chunk:
                    break
                d.write(chunk)
                n += 1
                if n % 64 == 1:
                    check_room(dest)
        tick(1)
    else:
        raise RuntimeError("Format not supported (install py7zr / rarfile)")


def gather(paths):
    items = []
    for p in paths:
        p = os.path.abspath(p)
        base = os.path.dirname(p)
        items.append((p, os.path.basename(p)))
        if os.path.isdir(p):
            for r, ds, fs in os.walk(p):
                for n in ds + fs:
                    f = os.path.join(r, n)
                    items.append((f, os.path.relpath(f, base)))
    return items


def uses_engine_create(fmt, pwd):
    return (fmt == "zip" and bool(pwd)) or (fmt == "7z" and not py7zr)


def create_archive(out, paths, fmt, level, pwd, tick, cancelled):
    if uses_engine_create(fmt, pwd):
        if not engine_path():
            raise RuntimeError("A password-protected ZIP, or a 7z without py7zr, needs the 7-Zip engine.\n"
                               "Open Help > Supported formats and click 'Get 7-Zip engine'.")
        return engine_create(out, paths, fmt, level, pwd, tick, cancelled)
    if fmt == "tar.zst":
        if not ZSTD:
            raise RuntimeError("tar.zst needs Python 3.14 or:  pip install zstandard")
        w = zstd_writer(out, level)
        try:
            with tarfile.open(fileobj=w, mode="w|") as t:
                for full, arc in gather(paths):
                    if cancelled():
                        raise Cancelled
                    t.add(full, arc, recursive=False)
                    tick(1, arc)
        finally:
            w.close()
        return
    if fmt == "7z":
        with py7zr.SevenZipFile(out, "w", password=pwd or None) as z:
            for p in paths:
                if cancelled():
                    raise Cancelled
                if os.path.isdir(p):
                    z.writeall(p, os.path.basename(p))
                else:
                    z.write(p, os.path.basename(p))
                tick(1, os.path.basename(p))
    elif fmt == "zip":
        comp = zipfile.ZIP_STORED if level == 0 else zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(out, "w", comp, compresslevel=None if level == 0 else level) as z:
            for full, arc in gather(paths):
                if cancelled():
                    raise Cancelled
                z.write(full, arc)
                tick(1, arc)
    else:
        mode, kw = {"tar": ("w", {}),
                    "tar.gz": ("w:gz", {"compresslevel": max(1, level)}),
                    "tar.bz2": ("w:bz2", {"compresslevel": max(1, level)}),
                    "tar.xz": ("w:xz", {"preset": level})}[fmt]
        with tarfile.open(out, mode, **kw) as t:
            for full, arc in gather(paths):
                if cancelled():
                    raise Cancelled
                t.add(full, arc, recursive=False)
                tick(1, arc)


def temp_beside(path):
    """A temporary name next to `path` (same disk, so the final rename is atomic) that nobody can guess:
    in a shared folder such as /tmp, a guessable name could be planted in advance as a link elsewhere.
    It ends with the real name, so tools that go by the extension (7-Zip) still see it."""
    d, name = os.path.split(os.path.abspath(path))
    return os.path.join(d, f".multizip_{uuid.uuid4().hex[:12]}_{name}")


def zip_rewrite(p, drop, add=None):
    """Copy a .zip without the entries drop(name) picks, let add(zipfile) write new ones, then swap it in.
    Entries are streamed, and the original stays untouched if anything fails or is cancelled."""
    tmp = temp_beside(p)
    try:
        with zipfile.ZipFile(p) as zi, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
            for i in zi.infolist():
                if drop(norm(i.filename)):
                    continue
                o = copy.copy(i)                        # writing updates offsets: keep the reader's info as it is
                if i.is_dir():
                    zo.writestr(o, b"")
                else:
                    with zi.open(i) as s, zo.open(o, "w", force_zip64=i.file_size >= zipfile.ZIP64_LIMIT) as d:
                        shutil.copyfileobj(s, d, 1024 * 1024)
            if add:
                add(zo)
            zo.comment = zi.comment
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):                         # something failed (e.g. encrypted entry): leave the original alone
            os.remove(tmp)


def zip_delete(p, names):
    zip_rewrite(p, wanted(names))


# ------------------------------------------------ create / delete files and folders
BAD_CHARS = '<>:"/\\|?*'
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def check_name(name):
    """Return an error message if `name` is not a valid Windows file name, else None."""
    n = name.strip()
    if not n:
        return "Type a name."
    if any(ch in n for ch in BAD_CHARS) or any(ord(ch) < 32 for ch in n):
        return 'A name cannot contain any of these characters:  < > : " / \\ | ? *'
    if n.endswith("."):
        return "A name cannot end with a dot."
    if n.split(".")[0].lower() in RESERVED:
        return f'"{n}" is a reserved Windows name.'
    if len(n) > 200:
        return "That name is too long."
    return None


def create_item(folder, name, is_dir):
    """Create an empty file or a folder. Never overwrites. Returns the new path."""
    err = check_name(name)
    if err:
        raise ValueError(err)
    path = os.path.join(folder, name.strip())
    if os.path.lexists(path):
        raise FileExistsError(f'"{name.strip()}" already exists here.')
    if is_dir:
        os.mkdir(path)
    else:
        with open(path, "xb"):
            pass
    return path


def zip_add_empty(arc, inner, name, is_dir):
    """Create an empty file or folder inside a .zip archive."""
    err = check_name(name)
    if err:
        raise ValueError(err)
    member = inner + name.strip() + ("/" if is_dir else "")
    with zipfile.ZipFile(arc) as z:
        if any(norm(n) == norm(member) for n in z.namelist()):
            raise FileExistsError(f'"{name.strip()}" already exists in this archive.')
    with zipfile.ZipFile(arc, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, b"")


def protected_reason(path):
    """Why this path must never be deleted from inside MultiZip (or None)."""
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    if len(p) <= 2:                                     # C:  or  /
        return "That is a whole drive."
    home = os.path.normcase(os.path.expanduser("~")).rstrip("\\/")
    guard = {home} | {os.path.normcase(os.path.join(home, d)) for d in
                      ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "AppData")}
    guard |= {os.path.normcase(d).rstrip("\\/") for d in desktop_dirs()}
    for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData", "SystemDrive"):
        v = os.environ.get(env)
        if v:
            guard.add(os.path.normcase(os.path.abspath(v + ("\\" if env == "SystemDrive" else ""))).rstrip("\\/"))
    if p in guard:
        return "That is an important system or personal folder."
    return None


def _rm_readonly(func, path, _exc):
    os.chmod(path, 0o700)
    func(path)


def delete_permanently(path):
    if os.path.islink(path) or getattr(os.path, "isjunction", lambda _p: False)(path):   # remove the link, never its target
        try:
            os.unlink(path)
        except (IsADirectoryError, PermissionError, OSError):
            os.rmdir(path)                              # directory symlink / junction
    elif os.path.isdir(path):
        shutil.rmtree(path, onerror=_rm_readonly)
    else:
        try:
            os.remove(path)
        except PermissionError:
            os.chmod(path, 0o700)                       # read-only file
            os.remove(path)


def recycle(paths):
    """Move to the Windows Recycle Bin (can be restored). Returns False if unavailable."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort),
                        ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", wintypes.LPCWSTR)]
        op = SHFILEOPSTRUCTW()
        op.wFunc = 3                                                  # FO_DELETE
        op.pFrom = "\0".join(os.path.normpath(p) for p in paths) + "\0\0"
        op.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400                # ALLOWUNDO NOCONFIRMATION SILENT NOERRORUI
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return rc == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


NO_7Z_ADD = ("Adding files into an existing .7z needs the py7zr library (version 0.20.2 or newer), which this "
             "copy of MultiZip does not have.\nYou can still open and extract it, or extract it and make a new 7z.")


def add_into(arc, paths, inner, pwd, tick, cancelled):
    """Append files/folders to an existing .zip, .7z or plain .tar."""
    k = kind(arc)
    if k == "7z":
        if not py7zr:
            raise RuntimeError(NO_7Z_ADD)
        with py7zr.SevenZipFile(arc, "a", password=pwd or None) as z:
            for p in paths:
                if cancelled():
                    raise Cancelled
                n = inner + os.path.basename(p)
                z.writeall(p, n) if os.path.isdir(p) else z.write(p, n)
                tick(1, os.path.basename(p))
        return
    items = [(full, inner + a.replace(os.sep, "/"), a) for full, a in gather(paths)]
    if k != "zip":
        with tarfile.open(arc, "a") as t:
            for full, n, a in items:
                if cancelled():
                    raise Cancelled
                t.add(full, n, recursive=False)
                tick(1, a)
        return
    with zipfile.ZipFile(arc) as z:
        if any(i.flag_bits & 1 for i in z.infolist()):
            raise RuntimeError("This .zip is password-protected. Files added by MultiZip would not be "
                               "protected, so nothing was added.")
        have = {norm(n) for n in z.namelist()}
    new = {norm(n) for full, n, _a in items if not os.path.isdir(full)}

    def put(z):
        for full, n, a in items:
            if cancelled():
                raise Cancelled
            if not (os.path.isdir(full) and norm(n) in have):      # an existing folder entry is kept as it is
                z.write(full, n)
            tick(1, a)
    if new & have:                                   # replace those files instead of storing a second copy
        zip_rewrite(arc, lambda n: n in new, put)
    else:
        with zipfile.ZipFile(arc, "a", zipfile.ZIP_DEFLATED) as z:
            put(z)


# ============================================================ UI helpers
def run_job(root, title, total, job):
    """Run job(tick, cancelled) in a thread behind a progress dialog."""
    dlg = tk.Toplevel(root)
    dlg.configure(bg=PAL["bg"])
    dlg.title(title)
    dlg.geometry("440x130")
    dlg.resizable(False, False)
    lab = ttk.Label(dlg, text="Working...")
    lab.pack(fill="x", padx=12, pady=(14, 4))
    bar = ttk.Progressbar(dlg, maximum=max(1, total))
    bar.pack(fill="x", padx=12)
    stop, q, res = threading.Event(), queue.Queue(), {}
    ttk.Button(dlg, text="Cancel", command=stop.set).pack(pady=10)
    dlg.protocol("WM_DELETE_WINDOW", stop.set)

    def work():
        try:
            job(lambda n=1, label=None: q.put(("t", n, label)), stop.is_set)
        except Cancelled:
            res["err"] = "Cancelled"
        except Exception as e:
            res["err"] = str(e) or type(e).__name__
        q.put(("end",))

    def poll():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "end":
                    dlg.destroy()
                    return
                bar["value"] = min(bar["maximum"], bar["value"] + m[1])
                if m[2]:
                    lab.config(text=m[2])
        except queue.Empty:
            pass
        dlg.after(50, poll)

    try:
        dlg.grab_set()
    except tk.TclError:
        pass
    dlg.focus_force()
    threading.Thread(target=work, daemon=True).start()
    poll()                                               # a tiny job can already be finished (dialog gone) here
    if dlg.winfo_exists():
        root.wait_window(dlg)
    return res.get("err")


def extract_many(root, archives, dest_root, subfolder, pwd, members=None, test=False):
    sizes = {a: unpacked_size(a, members) for a in archives}
    total = sum(sizes[a][0] if sizes[a] else 1 for a in archives)
    errs, need_pw, keep = [], [], False
    if not test:
        dests = [os.path.join(dest_root or os.path.dirname(a), stem(a)) if subfolder else (dest_root or os.path.dirname(a))
                 for a in archives]
        unpacked = [sizes[a][1] if sizes[a] else 0 for a in archives]
        msg = space_problem(unpacked, dests)
        if msg:
            messagebox.showerror(APP, msg, parent=root)
            return False
        packed = sum(os.path.getsize(a) for a in archives if os.path.isfile(a))
        if sum(unpacked) >= BOMB_BYTES and sum(unpacked) >= BOMB_RATIO * max(1, packed):
            if not messagebox.askyesno(APP, f"These archives are only {human(packed)} but would unpack to about "
                                            f"{human(sum(unpacked))}.\n\nThat is typical of a \"zip bomb\" made to "
                                            "fill your disk. Continue anyway?", icon="warning", default="no",
                                       parent=root):
                return False
        clash = [n for a, d in zip(archives, dests) for n in existing_targets(a, d, pwd, members)]
        if clash:
            ans = messagebox.askyesnocancel(
                APP, f"{len(clash)} file(s) already exist where these files are going, for example:\n  {clash[0]}\n\n"
                     "Replace them?\n\nYes  =  replace them with the archive's copies\n"
                     "No  =  keep your files and skip those\nCancel  =  stop", icon="warning", default="no",
                parent=root)
            if ans is None:
                return False
            keep = not ans

    def job(tick, cancelled):
        for a in archives:
            if cancelled():
                raise Cancelled
            tick(0, os.path.basename(a))
            base = dest_root or os.path.dirname(a)
            dest = os.path.join(base, stem(a)) if subfolder else base
            try:
                if test and backend(a) == "engine":
                    engine_test(a, pwd, tick, cancelled)
                elif test:
                    with tempfile.TemporaryDirectory() as tmp:
                        extract(a, tmp, pwd, None, tick, cancelled, mark=False)
                else:
                    extract(a, dest, pwd, members, tick, cancelled, keep=keep)
            except Cancelled:
                raise
            except Exception as e:
                t = str(e) or type(e).__name__
                if not pwd and ("password" in t.lower() or "encrypted" in t.lower()):
                    need_pw.append(a)
                else:
                    errs.append(f"{os.path.basename(a)}: {t}")

    err = run_job(root, "Testing" if test else "Extracting", total, job)
    if need_pw and not err:
        p = simpledialog.askstring(APP, "Enter password:", show="*", parent=root)
        if p:
            ok = extract_many(root, need_pw, dest_root, subfolder, p, members, test)
            if errs:                                     # don't lose the errors from the other archives
                messagebox.showerror(APP, "\n".join(errs), parent=root)
            return ok and not errs
        errs += [f"{os.path.basename(a)}: skipped - it needs a password" for a in need_pw]
    msgs = errs + ([err] if err and err != "Cancelled" else [])
    if msgs:
        messagebox.showerror(APP, "\n".join(msgs), parent=root)
    elif test and not err:
        messagebox.showinfo(APP, "No errors found.", parent=root)
    return not msgs and not err


# ============================================================ settings & themes
THEMES = {
    "Light":          dict(bg="#f3f4f6", fg="#1f2937", accent="#2563eb", list_bg="#ffffff", list_fg="#111827"),
    "Dark":           dict(bg="#1f2229", fg="#e5e7eb", accent="#3b82f6", list_bg="#15171c", list_fg="#e5e7eb"),
    "Midnight":       dict(bg="#0b1020", fg="#dbe4ff", accent="#7c5cff", list_bg="#10183a", list_fg="#dbe4ff"),
    "Forest":         dict(bg="#e6efe3", fg="#1f3326", accent="#2f855a", list_bg="#f7fbf5", list_fg="#1f3326"),
    "Ocean":          dict(bg="#e3f0f7", fg="#10324a", accent="#0e7490", list_bg="#f6fbfe", list_fg="#10324a"),
    "Rose":           dict(bg="#fbeaee", fg="#4a1f2b", accent="#db2777", list_bg="#fffafb", list_fg="#4a1f2b"),
    "Solarized Dark": dict(bg="#002b36", fg="#93a1a1", accent="#268bd2", list_bg="#073642", list_fg="#eee8d5"),
    "High Contrast":  dict(bg="#000000", fg="#ffffff", accent="#ffd400", list_bg="#000000", list_fg="#ffffff"),
    "Neon":           dict(bg="#0a0a0f", fg="#fcee0a", accent="#02d7f2", list_bg="#11111a", list_fg="#f2e94e"),
}
THEME_FONTS = {"Neon": ("@neon", True)}   # preset -> (font choice, UPPERCASE labels)
RENAMED = {"Cyberpunk 2077": "Neon", "@cyberpunk": "@neon"}   # names saved by older versions
THEME_KEYS = ("bg", "fg", "accent", "list_bg", "list_fg")
LEVELS = {"Store (no compression)": 0, "Fast": 1, "Normal": 6, "Best (slowest)": 9}
DEFAULTS = dict(theme="Light", colors=THEMES["Light"], custom={}, bg_image="", bg_dim=35,
                start_home=True, fmt="zip", level=6, reveal=True, font="@default", font_scale=1.0, caps=False, bg_fit="Fill",
                dl_folder="", dl_conns=8, dl_active=3, dl_limit=0, dl_extract=False)
FONT_SIZES = {"Small": 0.9, "Normal": 1.0, "Large": 1.15, "Extra large": 1.3}
NEON_FONTS = ("Rajdhani", "Orbitron", "Bahnschrift", "Segoe UI")
FONT = {"family": "Segoe UI", "scale": 1.0}
FONT_LABELS = {"@default": "System default", "@neon": "Neon style"}
BG_FITS = {"Fill": "Fill (crop to cover the window)", "Fit": "Fit (show the whole picture)",
           "Stretch": "Stretch (distort to fill)"}
IMAGE_TYPES = "*.png *.jpg *.jpeg *.jfif *.webp *.gif *.bmp *.tif *.tiff"


def to_photo(im):
    """PIL image -> Tk image. Works even when PIL.ImageTk is unavailable."""
    if ImageTk:
        try:
            return ImageTk.PhotoImage(im)
        except Exception:
            pass
    import base64, io
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()))


def fnt(size, bold=False):
    """Font tuple using the user's chosen family and text size."""
    sz = max(6, round(size * FONT["scale"]))
    return (FONT["family"], sz, "bold") if bold else (FONT["family"], sz)


def installed_fonts():
    return sorted({f for f in tkfont.families() if not f.startswith("@")}, key=str.lower)


def resolve_font(choice):
    have = {f.lower(): f for f in installed_fonts()}
    if choice == "@neon":
        for f in NEON_FONTS:
            if f.lower() in have:
                return have[f.lower()]
    elif choice and not choice.startswith("@") and choice.lower() in have:
        return have[choice.lower()]
    return have.get("segoe ui", "Segoe UI")


def fonts_dir():
    return os.path.join(os.path.dirname(settings_path()), "fonts")


def register_font(path):
    if os.name != "nt":
        return False
    try:
        import ctypes
        return ctypes.windll.gdi32.AddFontResourceExW(path, 0x10, 0) > 0   # private to this app
    except Exception:
        return False


def load_user_fonts():
    d = fonts_dir()
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.lower().endswith((".ttf", ".otf")):
                register_font(os.path.join(d, f))


def import_font_file(src):
    """Copy a .ttf/.otf into MultiZip's folder, load it, and return its family name."""
    if os.name != "nt":
        raise RuntimeError("Importing font files works on Windows.")
    os.makedirs(fonts_dir(), exist_ok=True)
    dest = os.path.join(fonts_dir(), os.path.basename(src))
    if os.path.abspath(src) != os.path.abspath(dest):
        shutil.copy2(src, dest)
    if not register_font(dest):
        raise RuntimeError("Windows could not load this font file.")
    try:
        return ImageFont.truetype(dest, 12).getname()[0] if ImageFont else os.path.splitext(os.path.basename(src))[0]
    except Exception:
        return os.path.splitext(os.path.basename(src))[0]


def settings_path():
    """MultiZip's own folder: %APPDATA% on Windows, ~/Library/Application Support on macOS, ~/.config on Linux."""
    if os.name == "nt":
        d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "MultiZip")
    else:
        base = (os.path.expanduser("~/Library/Application Support") if sys.platform == "darwin"
                else os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))
        d = os.path.join(base, "MultiZip")
        old = os.path.join(os.path.expanduser("~"), "MultiZip")          # where earlier versions kept it
        if not os.path.exists(d) and os.path.isfile(os.path.join(old, "settings.json")):
            try:
                os.makedirs(base, exist_ok=True)
                shutil.move(old, d)
                _moved_settings(old, d)
            except OSError:
                d = old
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


def _moved_settings(old, new):
    """After the folder moved, point the saved background picture at its new place."""
    f = os.path.join(new, "settings.json")
    try:
        with open(f, encoding="utf-8") as fh:
            s = json.load(fh)
        p = s.get("bg_image") if isinstance(s, dict) else None
        if isinstance(p, str) and p.startswith(old + os.sep):
            s["bg_image"] = new + p[len(old):]
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(s, fh, indent=2)
    except (OSError, ValueError):
        pass


def valid_theme(t):
    return isinstance(t, dict) and all(
        isinstance(t.get(k), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", t[k]) for k in THEME_KEYS)


def load_settings():
    s = dict(DEFAULTS)
    s["custom"] = {}
    try:
        with open(settings_path(), encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    for k, v in DEFAULTS.items():                       # a damaged or hand-edited file: wrong types fall back
        want, val = (int, float) if isinstance(v, float) else type(v), s.get(k)
        if not isinstance(val, want) or (isinstance(val, bool) and not isinstance(v, bool)):
            s[k] = dict(v) if isinstance(v, dict) else v
    s["theme"], s["font"] = RENAMED.get(s["theme"], s["theme"]), RENAMED.get(s["font"], s["font"])
    if not valid_theme(s.get("colors")):
        s["colors"] = dict(THEMES["Light"])
    s["custom"] = {k: v for k, v in s["custom"].items() if valid_theme(v)}
    for k, lo, hi in (("dl_conns", 1, 16), ("dl_active", 1, 10), ("dl_limit", 0, 10 ** 7), ("level", 0, 9),
                      ("bg_dim", 0, 90)):
        try:
            s[k] = max(lo, min(hi, int(s[k])))
        except (TypeError, ValueError):
            s[k] = DEFAULTS[k]
    return s


def save_settings(s):
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def _rgb(h):
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def _hex(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in c)


def mix(a, b, t):
    return _hex([x + (y - x) * t for x, y in zip(_rgb(a), _rgb(b))])


def lum(h):
    r, g, b = _rgb(h)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def on(h):
    return "#ffffff" if lum(h) < 0.6 else "#111111"


def palette(t):
    bg, fg, ac, lb, lf = t["bg"], t["fg"], t["accent"], t["list_bg"], t["list_fg"]
    return dict(bg=bg, fg=fg, accent=ac, lbg=lb, lfg=lf, btn=mix(bg, fg, .10), border=mix(bg, fg, .28),
                hdr=mix(lb, fg, .10), zebra=mix(lb, lf, .05), muted=mix(lb, lf, .55), muted_bg=mix(bg, fg, .55),
                accent_fg=on(ac), accent_hi=mix(ac, "#ffffff", .18))


PAL = palette(THEMES["Light"])


def level_name(v):
    return min(LEVELS, key=lambda k: abs(LEVELS[k] - v))


def formats():
    return (["zip"] + (["7z"] if (py7zr or engine_path()) else []) + ["tar", "tar.gz", "tar.bz2", "tar.xz"]
            + (["tar.zst"] if ZSTD else []))


def suggest_name(paths, fmt):
    if not paths:
        return ""
    p0 = os.path.abspath(paths[0])
    d = os.path.dirname(p0)
    if len(paths) == 1:
        base = os.path.basename(p0) if os.path.isdir(p0) else os.path.splitext(os.path.basename(p0))[0]
    else:
        base = os.path.basename(d) or "archive"
    return os.path.join(d, base + "." + fmt)


def list_drives():
    """[(menu text, path)] for 'This PC' and every drive / device, refreshed each time it is called."""
    out = [("This PC  (all drives)", "")]
    kinds = {2: "Removable drive", 3: "Local disk", 4: "Network drive", 5: "CD / DVD drive", 6: "RAM disk"}

    def usage(path):
        try:
            u = shutil.disk_usage(path)
            return f"{human(u.free)} free of {human(u.total)}"
        except OSError:
            return "not ready"
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            old = k32.SetErrorMode(1)                          # no "insert a disk" pop-ups
            try:
                mask = k32.GetLogicalDrives()
                for i in range(26):
                    if not mask & (1 << i):
                        continue
                    letter = chr(65 + i)
                    root = f"{letter}:\\"
                    t = k32.GetDriveTypeW(root)
                    name = ""
                    if t == 4:                                 # network: show the share, skip slow probing
                        buf, size = ctypes.create_unicode_buffer(520), ctypes.c_ulong(520)
                        try:
                            if ctypes.windll.mpr.WNetGetConnectionW(f"{letter}:", buf, ctypes.byref(size)) == 0:
                                name = buf.value
                        except Exception:
                            pass
                        info = name or kinds[4]
                    else:
                        vol = ctypes.create_unicode_buffer(261)
                        if t != 5 and k32.GetVolumeInformationW(root, vol, 261, None, None, None, None, 0):
                            name = vol.value
                        info = f"{name or kinds.get(t, 'Drive')}  ({kinds.get(t, 'Drive')})" if name else kinds.get(t, "Drive")
                        if t in (2, 3, 6):
                            info += "  -  " + usage(root)
                        elif t == 5:
                            info += "  -  " + ("not ready" if not os.path.isdir(root) else usage(root))
                    out.append((f"{root}   {info}", root))
            finally:
                k32.SetErrorMode(old)
            return out
        except Exception:
            out = out[:1]
            out += [(f"{c}:\\", f"{c}:\\") for c in string.ascii_uppercase if os.path.exists(c + ":\\")]
            return out
    # macOS / Linux
    def ls(d):                                              # other users' /media/<user> folders are unreadable
        try:
            return sorted(os.listdir(d))
        except OSError:
            return []
    cands = ["/"] + [os.path.join(b, n) for b in ("/Volumes", "/media", "/mnt", "/run/media") for n in ls(b)]
    for b in ("/media", "/run/media"):                      # /media/<user>/<disk>
        for u in ls(b):
            cands += [os.path.join(b, u, n) for n in ls(os.path.join(b, u))]
    for p in dict.fromkeys(cands):
        if os.path.isdir(p) and (p == "/" or os.path.ismount(p)):
            out.append((f"{p}   {'System disk' if p == '/' else os.path.basename(p)}  -  {usage(p)}", p))
    return out


def open_path(p):
    """Open a file or folder as a double-click in the system's file manager would. Raises OSError if it can't."""
    if os.name == "nt":
        os.startfile(p)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal_file(p):
    """Show the file selected in its folder (Linux file managers can only open the folder)."""
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select," + os.path.normpath(p)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", p])
        else:
            open_path(os.path.dirname(os.path.abspath(p)))
    except Exception:
        pass


def open_folder(p):
    try:
        if os.path.isdir(p):
            open_path(p)
    except Exception:
        pass


def bind_right_click(widget, fn):
    """Older Tk on macOS (Aqua) reports the right button as Button-2; everywhere else it is Button-3."""
    for ev in ("<Button-3>", "<Button-2>") if widget.tk.call("tk", "windowingsystem") == "aqua" else ("<Button-3>",):
        widget.bind(ev, fn)


def bind_wheel(widget, fn):
    """Call fn(lines) for the mouse wheel. Older Tk on Linux (X11) sends buttons 4 / 5 instead of wheel events."""
    widget.bind("<MouseWheel>", lambda e: fn(wheel_lines(e.delta)))
    if widget.tk.call("tk", "windowingsystem") == "x11":
        widget.bind("<Button-4>", lambda e: fn(-1))
        widget.bind("<Button-5>", lambda e: fn(1))


def wheel_lines(delta):
    """Windows sends +-120 per notch; macOS and touchpads send small deltas, which must still scroll."""
    if abs(delta) >= 120:
        return -int(delta / 120)
    return -1 if delta > 0 else (1 if delta < 0 else 0)


class Tip:
    def __init__(self, w, text):
        self.w, self.text, self.tw, self.job = w, text, None, None
        w.bind("<Enter>", self.sched, add="+")
        w.bind("<Leave>", self.hide, add="+")
        w.bind("<ButtonPress>", self.hide, add="+")

    def sched(self, _):
        self.job = self.w.after(600, self.show)

    def show(self):
        self.tw = tk.Toplevel(self.w)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry(f"+{self.w.winfo_rootx() + 8}+{self.w.winfo_rooty() + self.w.winfo_height() + 4}")
        tk.Label(self.tw, text=self.text, bg=PAL["hdr"], fg=PAL["fg"], relief="solid", bd=1,
                 padx=6, pady=3, font=fnt(9)).pack()

    def hide(self, _=None):
        if self.job:
            self.w.after_cancel(self.job)
            self.job = None
        if self.tw:
            self.tw.destroy()
            self.tw = None


# ================================================================= dialogs
class Dialog(tk.Toplevel):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.configure(bg=PAL["bg"])
        self.title(title)
        self.resizable(False, False)
        self.result = None
        self.columnconfigure(1, weight=1)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def finish(self, row, ok_text, cmd):
        f = ttk.Frame(self)
        f.grid(row=row, column=0, columnspan=3, pady=12)
        ttk.Button(f, text=ok_text, command=cmd, style="Accent.TButton").pack(side="left", padx=4)
        ttk.Button(f, text="Cancel", command=self.destroy).pack(side="left", padx=4)
        self.grab_set()
        self.focus_force()


class ExtractDialog(Dialog):
    def __init__(self, parent, default, ask_sub, S):
        super().__init__(parent, "Extract")
        self.dest, self.pwd = tk.StringVar(value=default), tk.StringVar()
        self.sub, self.reveal = tk.BooleanVar(value=ask_sub), tk.BooleanVar(value=S["reveal"])
        pad = dict(padx=10, pady=5)
        ttk.Label(self, text="Extract to:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.dest, width=54).grid(row=0, column=1, sticky="ew")
        ttk.Button(self, text="...", width=3, command=lambda: self.dest.set(
            filedialog.askdirectory(parent=self) or self.dest.get())).grid(row=0, column=2, padx=6)
        ttk.Checkbutton(self, text="Put each archive in its own folder", variable=self.sub).grid(
            row=1, column=1, sticky="w", **pad)
        ttk.Label(self, text="Password:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.pwd, show="*").grid(row=2, column=1, sticky="ew")
        ttk.Label(self, text="(only if the archive is protected)").grid(row=3, column=1, sticky="w", padx=10)
        ttk.Checkbutton(self, text="Show the folder when finished", variable=self.reveal).grid(
            row=4, column=1, sticky="w", **pad)
        self.finish(5, "Extract", self.ok)

    def ok(self):
        self.result = (self.dest.get().strip(), self.pwd.get(), self.sub.get(), self.reveal.get())
        self.destroy()


class AddDialog(Dialog):
    def __init__(self, parent, paths, S):
        super().__init__(parent, "Create archive")
        self.S, self.paths, self.edited = S, [], False
        fm = formats()
        self.fmt = tk.StringVar(value=S["fmt"] if S["fmt"] in fm else "zip")
        self.lvl, self.out, self.pwd = tk.StringVar(value=level_name(S["level"])), tk.StringVar(), tk.StringVar()
        self.reveal = tk.BooleanVar(value=S["reveal"])
        pad = dict(padx=10, pady=4)
        ttk.Label(self, text="1.  What do you want to archive?  (you can also drag files in)",
                  font=fnt(10, True)).grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        box = ttk.Frame(self)
        box.grid(row=1, column=0, columnspan=3, sticky="nsew", **pad)
        box.columnconfigure(0, weight=1)
        self.lb = tk.Listbox(box, height=7, width=64, selectmode="extended", bg=PAL["lbg"], fg=PAL["lfg"],
                             selectbackground=PAL["accent"], selectforeground=PAL["accent_fg"], relief="flat",
                             highlightthickness=1, highlightbackground=PAL["border"],
                             highlightcolor=PAL["accent"], font=fnt(10), activestyle="none")
        self.lb.grid(row=0, column=0, sticky="nsew")
        side = ttk.Frame(box)
        side.grid(row=0, column=1, padx=(8, 0), sticky="n")
        for t, c in (("Add files...", self.add_files), ("Add folder...", self.add_folder),
                     ("Remove", self.remove)):
            ttk.Button(side, text=t, command=c, width=14).pack(pady=2)
        self.count = ttk.Label(self, text="")
        self.count.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(self, text="2.  Name and options", font=fnt(10, True)).grid(
            row=3, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(self, text="Save as:").grid(row=4, column=0, sticky="w", **pad)
        e = ttk.Entry(self, textvariable=self.out)
        e.grid(row=4, column=1, sticky="ew")
        e.bind("<Key>", lambda _: setattr(self, "edited", True))
        ttk.Button(self, text="...", width=3, command=self.browse).grid(row=4, column=2, padx=6)
        ttk.Label(self, text="Format:").grid(row=5, column=0, sticky="w", **pad)
        cb = ttk.Combobox(self, textvariable=self.fmt, values=fm, state="readonly", width=12)
        cb.grid(row=5, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", self.on_fmt)
        ttk.Label(self, text="Compression:").grid(row=6, column=0, sticky="w", **pad)
        ttk.Combobox(self, textvariable=self.lvl, values=list(LEVELS), state="readonly",
                     width=22).grid(row=6, column=1, sticky="w")
        ttk.Label(self, text="Password:").grid(row=7, column=0, sticky="w", **pad)
        self.pe = ttk.Entry(self, textvariable=self.pwd, show="*", state="disabled")
        self.pe.grid(row=7, column=1, sticky="ew")
        ttk.Label(self, text="(7z, or zip with the 7-Zip engine)").grid(row=7, column=2, padx=6)
        ttk.Checkbutton(self, text="Show the archive in its folder when finished",
                        variable=self.reveal).grid(row=8, column=1, sticky="w", **pad)
        self.finish(9, "Create archive", self.ok)
        if DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self.on_drop)
        self.add_paths(paths)
        self.on_fmt()

    def on_drop(self, event):
        self.add_paths(self.tk.splitlist(event.data))
        return event.action

    def add_files(self):
        self.add_paths(filedialog.askopenfilenames(parent=self, title="Choose files"))

    def add_folder(self):
        d = filedialog.askdirectory(parent=self, title="Choose a folder")
        if d:
            self.add_paths([d])

    def add_paths(self, ps):
        for p in ps:
            p = os.path.abspath(p)
            if p not in self.paths and os.path.exists(p):
                self.paths.append(p)
                self.lb.insert("end", ("[folder]  " if os.path.isdir(p) else "") + p)
        self.refresh_info()

    def remove(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)
            del self.paths[i]
        self.refresh_info()

    def refresh_info(self):
        n = len(self.paths)
        self.count.config(text=f"{n} item(s) will be archived." if n
                          else "Nothing added yet - use the buttons or drag files into this window.")
        if not self.edited:
            self.out.set(suggest_name(self.paths, self.fmt.get()))

    def browse(self):
        f = filedialog.asksaveasfilename(parent=self, initialfile=os.path.basename(self.out.get()),
                                         initialdir=os.path.dirname(self.out.get()) or None)
        if f:
            self.out.set(f)
            self.edited = True

    def on_fmt(self, _=None):
        f, base = self.fmt.get(), self.out.get()
        for e in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".zip", ".7z", ".tar"):
            if base.lower().endswith(e):
                base = base[: -len(e)]
                break
        if base:
            self.out.set(base + "." + f)
        self.pe.config(state="normal" if (f == "7z" or (f == "zip" and engine_path())) else "disabled")

    def ok(self):
        out = self.out.get().strip()
        if not self.paths:
            return messagebox.showinfo(APP, "Add at least one file or folder first.", parent=self)
        if not out:
            return messagebox.showinfo(APP, "Choose a name for the archive.", parent=self)
        if os.path.exists(out) and not messagebox.askyesno(
                APP, f"{os.path.basename(out)} already exists. Replace it?", parent=self):
            return
        self.result = dict(paths=list(self.paths), out=out, fmt=self.fmt.get(),
                           lvl=LEVELS[self.lvl.get()],
                           pwd=self.pwd.get() if str(self.pe.cget("state")) == "normal" else "",
                           reveal=self.reveal.get())
        self.destroy()


class SettingsDialog(Dialog):
    KEYS = (("Window background", "bg"), ("Text", "fg"), ("Accent color", "accent"),
            ("List background", "list_bg"), ("List text", "list_fg"))

    def __init__(self, app):
        super().__init__(app, "Settings")
        self.app, self.S = app, app.S
        self.cur = dict(self.S["colors"])
        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, columnspan=3, padx=10, pady=10, sticky="nsew")
        ap, fo, gen = ttk.Frame(nb, padding=14), ttk.Frame(nb, padding=14), ttk.Frame(nb, padding=14)
        nb.add(ap, text="  Themes && backgrounds  ".replace("&&", "&"))
        nb.add(fo, text="  Fonts  ")
        nb.add(gen, text="  General  ")
        ttk.Label(ap, text="Theme:").grid(row=0, column=0, sticky="w", pady=3)
        self.tname = tk.StringVar(value=self.S["theme"])
        self.combo = ttk.Combobox(ap, textvariable=self.tname, state="readonly", width=24)
        self.combo.grid(row=0, column=1, columnspan=2, sticky="w")
        self.combo.bind("<<ComboboxSelected>>", self.pick_preset)
        self.sw = {}
        for i, (label, key) in enumerate(self.KEYS, start=1):
            ttk.Label(ap, text=label + ":").grid(row=i, column=0, sticky="w", pady=2)
            b = tk.Button(ap, width=12, relief="solid", bd=1, command=lambda k=key: self.pick_color(k))
            b.grid(row=i, column=1, sticky="w", pady=2)
            self.sw[key] = b
        ttk.Label(ap, text="Save as:").grid(row=6, column=0, sticky="w", pady=(10, 2))
        self.newname = tk.StringVar()
        ttk.Entry(ap, textvariable=self.newname, width=20).grid(row=6, column=1, sticky="w", pady=(10, 2))
        ttk.Button(ap, text="Save theme", command=self.save_theme).grid(row=6, column=2, padx=6, pady=(10, 2))
        row = ttk.Frame(ap)
        row.grid(row=7, column=0, columnspan=3, sticky="w", pady=2)
        for t, c in (("Delete theme", self.del_theme), ("Import...", self.import_theme),
                     ("Export...", self.export_theme)):
            ttk.Button(row, text=t, command=c).pack(side="left", padx=(0, 6))
        ttk.Separator(ap).grid(row=8, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(ap, text="Background image:").grid(row=9, column=0, sticky="w")
        self.bgl = ttk.Label(ap, text="", width=34)
        self.bgl.grid(row=9, column=1, columnspan=2, sticky="w")
        row = ttk.Frame(ap)
        row.grid(row=10, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Button(row, text="Choose image...", command=self.choose_bg).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Remove", command=self.remove_bg).pack(side="left")
        ttk.Label(ap, text="Darken:").grid(row=11, column=0, sticky="w")
        self.dim = ttk.Scale(ap, from_=0, to=90, length=200)
        self.dim.set(self.S["bg_dim"])
        self.dim.grid(row=11, column=1, columnspan=2, sticky="w")
        self.dim.bind("<ButtonRelease-1>", self.on_dim)
        ttk.Label(ap, text="Scaling:").grid(row=12, column=0, sticky="w", pady=(4, 0))
        self.fitv = tk.StringVar(value=BG_FITS.get(self.S.get("bg_fit"), BG_FITS["Fill"]))
        fc = ttk.Combobox(ap, textvariable=self.fitv, values=list(BG_FITS.values()), state="readonly", width=30)
        fc.grid(row=12, column=1, columnspan=2, sticky="w", pady=(4, 0))
        fc.bind("<<ComboboxSelected>>", self.on_fit)
        ttk.Label(ap, text="Always scales to the window size. JPG, PNG, WEBP, GIF and BMP work." + (
            "" if Image else "\n(Scaling and JPG/WEBP/BMP need Pillow - rebuild with build.bat.)"),
            style="Sub.TLabel").grid(
            row=13, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(ap, style="Sub.TLabel", justify="left", text=(
            f"Scaling engine: ON (Pillow {PIL_VER})" if Image else
            "Scaling engine: OFF - Pillow is missing in this build, so the picture is shown\n"
            "at its original size. Rebuild with build.bat (installs Pillow) or: pip install pillow")).grid(
            row=14, column=0, columnspan=3, sticky="w", pady=(2, 0))

        self.v_reveal = tk.BooleanVar(value=self.S["reveal"])
        self.v_fmt = tk.StringVar(value=self.S["fmt"] if self.S["fmt"] in formats() else "zip")
        self.v_lvl = tk.StringVar(value=level_name(self.S["level"]))
        ttk.Checkbutton(gen, text="Show the folder when an archive is created or extracted",
                        variable=self.v_reveal).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(gen, text="Default archive format:").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Combobox(gen, textvariable=self.v_fmt, values=formats(), state="readonly",
                     width=12).grid(row=2, column=1, sticky="w")
        ttk.Label(gen, text="Default compression:").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Combobox(gen, textvariable=self.v_lvl, values=list(LEVELS), state="readonly",
                     width=22).grid(row=3, column=1, sticky="w")

        self.build_fonts(fo)
        f = ttk.Frame(self)
        f.grid(row=1, column=0, columnspan=3, pady=(0, 12))
        ttk.Button(f, text="Done", style="Accent.TButton", command=self.done).pack()
        self.protocol("WM_DELETE_WINDOW", self.done)
        self.refresh_all()
        self.grab_set()
        self.focus_force()

    def names(self):
        return list(THEMES) + list(self.S["custom"])

    def refresh_all(self):
        self.combo.config(values=self.names())
        for k, b in self.sw.items():
            b.config(bg=self.cur[k], activebackground=self.cur[k], text=self.cur[k], fg=on(self.cur[k]))
        p = self.S.get("bg_image")
        self.bgl.config(text=(self.S.get("bg_name") or os.path.basename(p)) if p else "(none)")

    def apply(self):
        self.app.set_palette(self.cur)
        self.configure(bg=PAL["bg"])
        self.refresh_all()

    def pick_preset(self, _=None):
        name = self.tname.get()
        allt = {**THEMES, **self.S["custom"]}
        self.cur = dict(allt[name])
        if name in THEME_FONTS or (self.S["font"] == "@neon" and self.S["caps"]):
            self.S["font"], self.S["caps"] = THEME_FONTS.get(name, ("@default", False))
            self.fontv.set(self.font_label(self.S["font"]))
            self.capsv.set(self.S["caps"])
            self.app.refresh_fonts()
            self.update_preview()
        self.apply()

    # ---- fonts
    def font_label(self, v):
        return FONT_LABELS.get(v, v)

    def font_values(self):
        return list(FONT_LABELS.values()) + installed_fonts()

    def build_fonts(self, fo):
        S = self.S
        ttk.Label(fo, text="Font:").grid(row=0, column=0, sticky="w", pady=4)
        self.fontv = tk.StringVar(value=self.font_label(S["font"]))
        self.fcombo = ttk.Combobox(fo, textvariable=self.fontv, values=self.font_values(),
                                   state="readonly", width=32)
        self.fcombo.grid(row=0, column=1, sticky="w")
        self.fcombo.bind("<<ComboboxSelected>>", self.pick_font)
        ttk.Label(fo, text="Text size:").grid(row=1, column=0, sticky="w", pady=4)
        self.fsize = tk.StringVar(value=min(FONT_SIZES, key=lambda k: abs(FONT_SIZES[k] - float(S["font_scale"]))))
        c = ttk.Combobox(fo, textvariable=self.fsize, values=list(FONT_SIZES), state="readonly", width=14)
        c.grid(row=1, column=1, sticky="w")
        c.bind("<<ComboboxSelected>>", self.pick_font)
        self.capsv = tk.BooleanVar(value=bool(S["caps"]))
        ttk.Checkbutton(fo, text="UPPERCASE buttons and titles", variable=self.capsv,
                        command=self.pick_font).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Button(fo, text="Import font file (.ttf / .otf)...", command=self.import_font).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 2))
        self.preview = ttk.Label(fo, justify="left", text="The quick brown fox jumps over 0123456789\n"
                                 "ARCHIVE  /  EXTRACT  /  COMPRESS")
        self.preview.grid(row=4, column=0, columnspan=2, sticky="w", pady=(14, 4))
        ttk.Label(fo, style="Sub.TLabel", justify="left", text=(
            "Neon style uses Rajdhani or Orbitron if available\n"
            "(import the .ttf above), otherwise Bahnschrift.")).grid(row=5, column=0, columnspan=2, sticky="w")
        self.update_preview()

    def update_preview(self):
        self.preview.config(font=fnt(14))

    def pick_font(self, _=None):
        inv = {lab: k for k, lab in FONT_LABELS.items()}
        v = self.fontv.get()
        self.S["font"] = inv.get(v, v)
        self.S["font_scale"] = FONT_SIZES[self.fsize.get()]
        self.S["caps"] = self.capsv.get()
        self.app.refresh_fonts()
        self.update_preview()

    def import_font(self):
        f = filedialog.askopenfilename(parent=self, title="Choose a font file",
                                       filetypes=[("Font files", "*.ttf *.otf"), ("All files", "*.*")])
        if not f:
            return
        try:
            fam = import_font_file(f)
        except Exception as e:
            return messagebox.showerror(APP, str(e), parent=self)
        self.fcombo.config(values=self.font_values())
        self.fontv.set(fam)
        self.pick_font()
        if FONT["family"].lower() != fam.lower():
            messagebox.showinfo(APP, f"'{fam}' was imported. Restart MultiZip to start using it.", parent=self)

    def pick_color(self, k):
        c = colorchooser.askcolor(color=self.cur[k], parent=self, title="Choose a color")[1]
        if c:
            self.cur[k] = c
            self.tname.set("Custom")
            self.apply()

    def save_theme(self):
        n = self.newname.get().strip()
        if not n or n in THEMES:
            return messagebox.showinfo(APP, "Type a new name for your theme.", parent=self)
        self.S["custom"][n] = dict(self.cur)
        self.tname.set(n)
        self.newname.set("")
        self.refresh_all()

    def del_theme(self):
        n = self.tname.get()
        if n in self.S["custom"]:
            del self.S["custom"][n]
            self.tname.set("Custom")
            self.refresh_all()
        else:
            messagebox.showinfo(APP, "Only themes you saved yourself can be deleted.", parent=self)

    def import_theme(self):
        f = filedialog.askopenfilename(parent=self, filetypes=[("Theme file", "*.json")])
        if not f:
            return
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            colors = d.get("colors", d)
            if not valid_theme(colors):
                raise ValueError("Not a valid theme file.")
            n = str(d.get("name") or os.path.splitext(os.path.basename(f))[0])
            if n in THEMES:
                n += " (imported)"
            self.S["custom"][n] = {k: colors[k] for k in THEME_KEYS}
            self.tname.set(n)
            self.pick_preset()
        except Exception as e:
            messagebox.showerror(APP, f"Could not import theme:\n{e}", parent=self)

    def export_theme(self):
        f = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                         initialfile=(self.tname.get() or "theme") + ".json",
                                         filetypes=[("Theme file", "*.json")])
        if f:
            with open(f, "w", encoding="utf-8") as fh:
                json.dump({"name": self.tname.get(), "colors": self.cur}, fh, indent=2)

    def choose_bg(self):
        f = filedialog.askopenfilename(parent=self, title="Choose a background image", filetypes=[
            ("Images (JPG, PNG, WEBP, GIF, BMP)", IMAGE_TYPES), ("All files", "*.*")])
        if not f:
            return
        d = os.path.dirname(settings_path())
        ext = os.path.splitext(f)[1].lower()
        try:
            if Image:
                im = ImageOps.exif_transpose(Image.open(f))
                if im.mode in ("RGBA", "LA", "P") or "transparency" in im.info:
                    im = im.convert("RGBA")
                    im = Image.alpha_composite(Image.new("RGBA", im.size, PAL["bg"]), im)
                im = im.convert("RGB")
                im.thumbnail((2560, 2560))
                dest = os.path.join(d, "background.png")
                im.save(dest)
            elif ext in (".png", ".gif"):
                dest = os.path.join(d, "background" + ext)
                shutil.copy2(f, dest)
            else:
                raise RuntimeError("JPG, WEBP and BMP need Pillow.\nRebuild MultiZip with build.bat "
                                   "(it installs Pillow), or run:  pip install pillow")
            for old in os.listdir(d):
                if old.startswith("background.") and os.path.join(d, old) != dest:
                    os.remove(os.path.join(d, old))
            self.S["bg_image"], self.S["bg_name"] = dest, os.path.basename(f)
            self.app.redraw_backgrounds()
            self.refresh_all()
            if not Image:
                messagebox.showinfo(APP, "Pillow isn't installed in this build, so the picture cannot be "
                                         "scaled to the window.\nRebuild with build.bat (it installs Pillow).",
                                    parent=self)
        except Exception as e:
            messagebox.showerror(APP, f"Could not use that image:\n{e}", parent=self)

    def remove_bg(self):
        self.S["bg_image"], self.S["bg_name"] = "", ""
        self.app.redraw_backgrounds()
        self.refresh_all()

    def on_fit(self, _=None):
        self.S["bg_fit"] = self.fitv.get().split()[0]
        self.app.redraw_backgrounds()

    def on_dim(self, _=None):
        self.S["bg_dim"] = int(float(self.dim.get()))
        self.app.redraw_backgrounds()

    def done(self):
        S = self.S
        S.update(colors=dict(self.cur), theme=self.tname.get(), reveal=self.v_reveal.get(), fmt=self.v_fmt.get(), level=LEVELS[self.v_lvl.get()])
        save_settings(S)
        self.destroy()


def format_rows():
    eng = bool(engine_path())
    yes = lambda ok, no="Needs the 7-Zip engine": "Yes" if ok else no
    return [
        ("ZIP  (also JAR, APK, DOCX, XLSX, EPUB, CBZ ...)", "Yes", "Yes",
         "Built in. AES-256 encrypted ZIPs " + ("are supported." if eng else "need the 7-Zip engine.")),
        ("TAR, TGZ, TBZ2, TXZ", "Yes", "Yes", "Built in."),
        ("GZ, BZ2, XZ, LZMA  (single compressed files)", "Yes", "No", "Built in. To make them, use tar.gz / tar.xz."),
        ("ZSTD  (.zst, .tar.zst)", yes(ZSTD, "Python 3.14, or pip install zstandard"),
         yes(ZSTD, "Python 3.14, or pip install zstandard"), "Zstandard compression."),
        ("7Z", yes(py7zr or eng), yes(py7zr or eng), "Passwords and header encryption supported."),
        ("RAR, CBR", yes(eng), "No", "RAR can be read but never created: the format is proprietary (WinRAR only)."),
        ("ISO, CAB, WIM, VHD/VHDX, VMDK, VDI, DMG, ARJ, LZH, CPIO, RPM, DEB, XAR, SquashFS, "
         "CRAMFS, CHM, UDF, HFS, NTFS/FAT/EXT images, Z ...", yes(eng), "No", "Double-click opens them like folders."),
        ("Anything else: EXE/MSI installers, NSIS, split .001 files, any file that is secretly an archive",
         yes(eng), "No", "Right-click > Open as archive."),
    ]


class FormatsDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "Supported formats")
        self.app = app
        self.geometry("900x440")
        self.resizable(True, True)
        self.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(self, columns=("read", "make", "note"), show="headings", height=9)
        for c, t, w in (("#0", "", 0), ("read", "Open / extract", 150), ("make", "Create", 150), ("note", "Notes", 340)):
            if c != "#0":
                self.tree.heading(c, text=t)
                self.tree.column(c, width=w, anchor="w")
        self.tree["show"] = ("tree", "headings")
        self.tree.column("#0", width=330, stretch=False)
        self.tree.heading("#0", text="Format")
        self.tree.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=10, pady=(10, 4))
        self.status = ttk.Label(self, justify="left", wraplength=840)
        self.status.grid(row=1, column=0, columnspan=3, sticky="w", padx=12, pady=6)
        f = ttk.Frame(self)
        f.grid(row=2, column=0, columnspan=3, pady=(2, 12))
        self.getbtn = ttk.Button(f, text="Get 7-Zip engine", style="Accent.TButton", command=self.get)
        self.getbtn.pack(side="left", padx=4)
        ttk.Button(f, text="Close", command=self.destroy).pack(side="left", padx=4)
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for name, rd, mk, note in format_rows():
            self.tree.insert("", "end", text=name, values=(rd, mk, note))
        ep = engine_path()
        if ep:
            v = _ENGINE["ver"]
            self.status.config(text=f"7-Zip engine: ON  ({'%d.%02d' % v if v else ''})  {ep}\n"
                                    "Powered by 7-Zip (www.7-zip.org), GNU LGPL licence; unRAR code is restricted to "
                                    "extracting.")
            self.getbtn.pack_forget()
        else:
            self.status.config(text="7-Zip engine: not found.  It unlocks RAR, ISO, CAB, WIM, VHD, DMG, ARJ, LZH, RPM, "
                                    "DEB, encrypted ZIP and many more." +
                                    ("" if os.name == "nt" else "\nInstall 7-Zip (7zz / 7z) with your package manager."))
            if os.name != "nt":
                self.getbtn.pack_forget()

    def get(self):
        if self.app.get_engine():
            self.refresh()


# ===================================================== disk usage + clean up (logic)
def _is_junction(e):
    """True for Windows junctions / mount points that must never be followed."""
    try:
        j = getattr(e, "is_junction", None)
        if j:
            return bool(j())
        if getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:      # reparse point
            return os.path.normcase(os.path.realpath(e.path)) != os.path.normcase(os.path.abspath(e.path))
    except OSError:
        return True
    return False


class DNode:
    """One folder in the disk-usage tree."""
    __slots__ = ("name", "path", "own", "own_files", "size", "files", "dirs")

    def __init__(self, name, path):
        self.name, self.path = name, path
        self.own = self.own_files = self.size = self.files = 0
        self.dirs = []

    def total(self):
        self.size, self.files = self.own, self.own_files
        for d in self.dirs:
            d.total()
            self.size += d.size
            self.files += d.files


def scan_usage(root, cancelled, progress, max_depth=7, top_n=300):
    """Measure a folder. Folders deeper than max_depth are folded into their parent row.
    Returns dict(root=DNode, ext={ext: [bytes, count]}, top=[(bytes, path, mtime)], stats)."""
    root = os.path.abspath(root)
    stats = dict(files=0, bytes=0, folders=0, skipped=0)
    ext, heap = {}, []
    # macOS / Linux: stay on this disk like `du -x` (/proc, /sys and other mounts are not files on it).
    # On Windows a mounted folder is a junction, which is skipped below anyway.
    dev = os.stat(root).st_dev if os.name != "nt" else None
    rootnode = DNode(os.path.basename(root.rstrip("\\/")) or root, root)
    stack, last = [(rootnode, root, 0)], time.time()
    while stack:
        if cancelled():
            raise Cancelled
        owner, path, depth = stack.pop()
        try:
            it = os.scandir(path)
        except OSError:
            stats["skipped"] += 1
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if _is_junction(e) or (dev is not None and e.stat(follow_symlinks=False).st_dev != dev):
                            continue
                        stats["folders"] += 1
                        if depth < max_depth:
                            child = DNode(e.name, e.path)
                            owner.dirs.append(child)
                            stack.append((child, e.path, depth + 1))
                        else:
                            stack.append((owner, e.path, depth))
                    else:
                        st = e.stat(follow_symlinks=False)
                        size = st.st_size
                        owner.own += size
                        owner.own_files += 1
                        stats["files"] += 1
                        stats["bytes"] += size
                        x = os.path.splitext(e.name)[1].lower() or "(no extension)"
                        rec = ext.setdefault(x, [0, 0])
                        rec[0] += size
                        rec[1] += 1
                        if len(heap) < top_n:
                            heapq.heappush(heap, (size, e.path, st.st_mtime))
                        elif size > heap[0][0]:
                            heapq.heapreplace(heap, (size, e.path, st.st_mtime))
                except OSError:
                    stats["skipped"] += 1
        if time.time() - last > 0.15:
            last = time.time()
            progress(stats, path)
    rootnode.total()
    return dict(root=rootnode, ext=ext, top=sorted(heap, reverse=True), stats=stats)


def disk_mounts(text):
    """Mount points of real disks in a Linux /proc/self/mounts text (no network shares, snaps, /proc ...)."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 3 or not f[0].startswith("/dev/") or f[0].startswith("/dev/loop") or f[2] == "squashfs":
            continue
        mnt = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), f[1])      # \040 = space ...
        if mnt not in out:
            out.append(mnt)
    return out


def local_drive_roots():
    """Every internal and removable drive that is ready (network drives are left out: too slow)."""
    if sys.platform == "darwin":                        # user files live on their own "Data" volume
        data = "/System/Volumes/Data"
        return [data if os.path.isdir(data) else "/"] + [p for _t, p in list_drives() if p.startswith("/Volumes/")]
    if os.name != "nt":
        try:
            with open("/proc/self/mounts", encoding="utf-8", errors="replace") as f:
                roots = [m for m in disk_mounts(f.read()) if os.path.isdir(m)]
        except OSError:
            roots = []
        return roots or ["/"]
    out = []
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        old = k32.SetErrorMode(1)
        try:
            mask = k32.GetLogicalDrives()
            for i in range(26):
                root = f"{chr(65 + i)}:\\"
                if mask & (1 << i) and k32.GetDriveTypeW(root) in (2, 3) and os.path.isdir(root):
                    out.append(root)
        finally:
            k32.SetErrorMode(old)
    except Exception:
        out = [f"{c}:\\" for c in string.ascii_uppercase if os.path.isdir(c + ":\\")]
    return out


def merge_usage(results, name="This PC  (all drives)"):
    """Combine several scan_usage() results under one virtual root."""
    root = DNode(name, "")
    ext, top = {}, []
    stats = dict(files=0, bytes=0, folders=0, skipped=0)
    for r in results:
        root.dirs.append(r["root"])
        for k in stats:
            stats[k] += r["stats"][k]
        for x, (b, n) in r["ext"].items():
            rec = ext.setdefault(x, [0, 0])
            rec[0] += b
            rec[1] += n
        top += r["top"]
    root.total()
    return dict(root=root, ext=ext, top=sorted(top, reverse=True)[:300], stats=stats)


def share_bar(pct, width=10):
    pct = max(0.0, min(100.0, pct))
    n = int(round(pct / 100 * width))
    return "\u2588" * n + "\u2591" * (width - n) + f"  {pct:.0f}%" if pct >= 1 else "\u2591" * width + "  <1%"


def iter_tree(top, cancelled=lambda: False):
    """Yield (path, stat, is_dir) for everything below `top`, directories AFTER their contents.
    Never follows symbolic links or junctions."""
    try:
        with os.scandir(top) as it:
            entries = list(it)
    except OSError:
        return
    for e in entries:
        if cancelled():
            raise Cancelled
        try:
            if e.is_symlink():
                yield e.path, e.stat(follow_symlinks=False), False
            elif e.is_dir(follow_symlinks=False):
                if _is_junction(e):
                    continue
                st = e.stat(follow_symlinks=False)           # taken BEFORE the contents are removed
                yield from iter_tree(e.path, cancelled)
                yield e.path, st, True
            else:
                yield e.path, e.stat(follow_symlinks=False), False
        except OSError:
            continue


def recycle_bin_info():
    """(bytes, items) in the Windows Recycle Bin, or None when unavailable."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        for pack in (1, 8):
            class SHQUERYRBINFO(ctypes.Structure):
                _pack_ = pack
                _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong),
                            ("i64NumItems", ctypes.c_longlong)]
            info = SHQUERYRBINFO()
            info.cbSize = ctypes.sizeof(info)
            if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
                return int(info.i64Size), int(info.i64NumItems)
    except Exception:
        pass
    return None


def empty_recycle_bin():
    try:
        import ctypes
        return ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x7) in (0, 1)   # no prompt/progress/sound
    except Exception:
        return False


def G(base, *parts):
    """Build a path pattern from a real folder plus wildcard parts (the folder itself is escaped)."""
    return os.path.join(glob.escape(base), *parts)


def expand_roots(roots, strict=True):
    """Existing folders matching the patterns. Never a drive root or the home folder itself; with `strict`
    (anything that deletes for good) never any protected folder either - TEMP may be set to e.g. Downloads."""
    out = []
    for r in roots:
        for p in glob.glob(r):
            q = os.path.normcase(os.path.abspath(p)).rstrip("\\/")
            if os.path.isdir(p) and len(q) > 3 and q != os.path.normcase(os.path.expanduser("~")).rstrip("\\/") \
                    and not (strict and protected_reason(p)) and p not in out:
                out.append(p)
    return out


def cleanup_categories():
    """The places the assistant knows are safe to clean. Nothing outside these is ever touched."""
    cats = []
    nt = os.name == "nt"
    tmp = tempfile.gettempdir()
    env = lambda k: os.environ.get(k, "")
    local, home = env("LOCALAPPDATA"), os.path.expanduser("~")

    def cat(cid, name, desc, roots=(), **kw):
        cats.append(dict(id=cid, name=name, desc=desc, roots=list(roots), hours=kw.pop("hours", 0),
                         mode=kw.pop("mode", "delete"), default=kw.pop("default", True), **kw))
    cat("temp", "Temporary files", "Leftovers from programs in your temp folder. Only files older than a day "
        "are removed, so running programs are not disturbed.", [G(tmp)], hours=24, exclude="multizip_")
    cat("multizip", "MultiZip leftovers", "Temporary folders made when you open a file from an archive or "
        "drag files out of one.", [G(tmp)], hours=1, include="multizip_")
    if nt:
        cat("recyclebin", "Recycle Bin", "Everything you deleted earlier. Emptying it cannot be undone.",
            mode="recyclebin")
        cat("browsers", "Web browser caches", "Saved web page data from Chrome, Edge, Brave and Firefox. "
            "Pages simply load fresh next time. Close your browsers first for the best result.",
            [G(local, vendor, "User Data", "*", sub) for vendor in ("Google\\Chrome", "Microsoft\\Edge",
             "BraveSoftware\\Brave-Browser") for sub in ("Cache", "Code Cache", "GPUCache")]
            + [G(local, "Mozilla", "Firefox", "Profiles", "*", "cache2")] if local else [])
        cat("inetcache", "Windows internet cache", "Web files cached by Windows and older apps.",
            [G(local, "Microsoft", "Windows", "INetCache")] if local else [])
        cat("crash", "Crash dumps and error reports", "Diagnostic files left behind after programs crashed.",
            [G(local, "CrashDumps"), G(local, "Microsoft", "Windows", "WER", "ReportArchive"),
             G(local, "Microsoft", "Windows", "WER", "ReportQueue")] if local else [])
        cat("thumbs", "Thumbnail cache", "Picture previews Windows keeps; they are rebuilt when needed. "
            "Files in use are skipped.", [G(local, "Microsoft", "Windows", "Explorer")] if local else [],
            patterns=["thumbcache_*.db", "iconcache_*.db"])
        cat("wintemp", "Windows temporary files", "System temp folder. Some files need administrator rights "
            "and are skipped.", [G(env("SystemRoot") or "C:\\Windows", "Temp")], hours=24,
            default=False)
        cat("downloads", "Old downloads (older than 90 days)", "Files you downloaded long ago. Moved to the "
            "Recycle Bin so you can still restore them. Have a look first - these are YOUR files.",
            [G(home, "Downloads")], hours=24 * 90, mode="recycle", default=False)
    else:
        cat("thumbs", "Thumbnail cache", "Picture previews, rebuilt when needed.",
            [G(home, ".cache", "thumbnails")])
    return [c for c in cats if c["mode"] == "recyclebin" or c["roots"]]


def _top_entries(cat):
    """Top-level entries of each root that this category is allowed to touch."""
    inc, exc, pats = cat.get("include"), cat.get("exclude"), cat.get("patterns")
    for root in expand_roots(cat["roots"], strict=cat["mode"] != "recycle"):   # "recycle" can be undone
        try:
            with os.scandir(root) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            n = e.name.lower()
            if inc and not n.startswith(inc):
                continue
            if exc and n.startswith(exc):
                continue
            if pats and not any(fnmatch.fnmatch(n, p) for p in pats):
                continue
            yield e


def _entry_size(e, cancelled):
    try:
        if e.is_dir(follow_symlinks=False) and not e.is_symlink():
            if _is_junction(e):
                return 0
            return sum(st.st_size for _p, st, d in iter_tree(e.path, cancelled) if not d)
        return e.stat(follow_symlinks=False).st_size
    except OSError:
        return 0


def _walk_category(cat, cancelled):
    """Yield (path, stat, is_dir) for everything below the allowed entries, contents before folders."""
    for e in _top_entries(cat):
        try:
            if e.is_symlink() or (e.is_dir(follow_symlinks=False) and _is_junction(e)):
                continue
            if e.is_dir(follow_symlinks=False):
                st = e.stat(follow_symlinks=False)           # taken BEFORE the contents are removed
                yield from iter_tree(e.path, cancelled)
                yield e.path, st, True
            else:
                yield e.path, e.stat(follow_symlinks=False), False
        except OSError:
            continue


def scan_category(cat, cancelled=lambda: False):
    """-> (bytes that would be freed, number of files/items)."""
    if cat["mode"] == "recyclebin":
        return recycle_bin_info() or (0, 0)
    cutoff = time.time() - cat["hours"] * 3600
    size = n = 0
    if cat["mode"] == "recycle":
        for e in _top_entries(cat):
            if cancelled():
                raise Cancelled
            try:
                if e.stat(follow_symlinks=False).st_mtime <= cutoff:
                    size += _entry_size(e, cancelled)
                    n += 1
            except OSError:
                pass
        return size, n
    for _p, st, isdir in _walk_category(cat, cancelled):
        if not isdir and st.st_mtime <= cutoff:
            size += st.st_size
            n += 1
    return size, n


def _remove_file(p):
    try:
        os.remove(p)
        return True
    except PermissionError:
        try:
            os.chmod(p, 0o700)
            os.remove(p)
            return True
        except OSError:
            return False
    except OSError:
        return False


def clean_category(cat, cancelled=lambda: False):
    """Remove what scan_category found. Returns (bytes freed, items removed, items skipped)."""
    if cat["mode"] == "recyclebin":
        info = recycle_bin_info() or (0, 0)
        return (info[0], info[1], 0) if empty_recycle_bin() else (0, 0, info[1])
    cutoff = time.time() - cat["hours"] * 3600
    freed = done = skipped = 0
    if cat["mode"] == "recycle":
        batch = []
        for e in _top_entries(cat):
            if cancelled():
                raise Cancelled
            try:
                if e.stat(follow_symlinks=False).st_mtime <= cutoff and not protected_reason(e.path):
                    batch.append((e.path, _entry_size(e, cancelled)))
            except OSError:
                pass
        for i in range(0, len(batch), 25):
            part = batch[i:i + 25]
            if recycle([p for p, _s in part]):
                freed += sum(s for _p, s in part)
                done += len(part)
            else:
                skipped += len(part)
        return freed, done, skipped
    for p, st, isdir in _walk_category(cat, cancelled):
        if st.st_mtime > cutoff:
            continue
        if isdir:
            try:
                os.rmdir(p)                                 # only succeeds when empty
            except OSError:
                pass
        elif _remove_file(p):
            freed += st.st_size
            done += 1
        else:
            skipped += 1
    return freed, done, skipped


# ===================================================== disk usage + clean up (windows)
def make_tree(parent, first, cols, selectmode="extended", height=12):
    """A Treeview with a scrollbar. cols = [(id, heading, width, anchor)]."""
    wrap = ttk.Frame(parent)
    wrap.pack(fill="both", expand=True)
    tv = ttk.Treeview(wrap, columns=[c[0] for c in cols], selectmode=selectmode, height=height)
    tv.heading("#0", text=first, anchor="w")
    tv.column("#0", width=380, stretch=True)
    for cid, head, width, anchor in cols:
        tv.heading(cid, text=head, anchor=anchor)
        tv.column(cid, width=width, anchor=anchor, stretch=False)
    sb = ttk.Scrollbar(wrap, command=tv.yview)
    tv.config(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tv.pack(fill="both", expand=True)
    return tv


def icon_rgba(black, white, n=16):
    """Rebuild an icon with real transparency from the same icon drawn on black and on white."""
    out = bytearray(n * n * 4)
    for i in range(n * n):
        o = i * 4
        # channel order is B, G, R, X
        diff = sum(255 - (white[o + c] - black[o + c]) for c in range(3)) / 3.0       # = alpha * 255
        a = max(0, min(255, int(round(diff))))
        for c in range(3):
            v = 0 if a == 0 else min(255, int(round(black[o + c] * 255.0 / a)))
            out[o + (2 - c)] = v                                                      # -> R, G, B
        out[o + 3] = a
    return bytes(out)


RESAMPLE = getattr(Image, "LANCZOS", None) or getattr(Image, "BICUBIC", None) if Image else None


class IconCache:
    """Real Windows icons (16x16) for files and folders. Does nothing on other systems."""
    OWN = {".exe", ".ico", ".lnk", ".scr", ".cur", ".msi"}          # files that carry their own icon

    def __init__(self):
        self.c = {}
        self.ok = os.name == "nt" and Image is not None

    def get(self, path, isdir=False, size=16):
        if not self.ok or not path:
            return None
        ext = os.path.splitext(path)[1].lower()
        own = not isdir and ext in self.OWN
        if isdir:
            drive = len(path.rstrip("\\/")) == 2
            key = ("drive", path[:2].upper(), size) if drive else ("dir", size)
        else:
            key = ("file", path.lower(), size) if own else ("ext", ext, size)
        if key not in self.c:
            attr = 0x10 if isdir else 0x80
            real = key[0] == "drive" or (own and os.path.isfile(path))      # ask the file itself when it exists
            ic = None
            for use_real in ((True, False) if real else (False,)):          # fall back to the generic type icon
                name = path if use_real else ("folder" if isdir else "file" + ext)
                try:
                    im = self.load(name, use_real, attr, size)
                    if im is not None and im.getchannel("A").getextrema()[1] == 0:
                        im = None                                           # came back fully transparent
                    if im is not None:
                        ic = to_photo(im)
                        break
                    self.log(key, "Windows gave no icon for " + name)
                except Exception:
                    self.log(key, f"{name}: " + traceback.format_exc()[-400:])
            self.c[key] = ic
        return self.c[key]

    def log(self, key, why):
        """Failures go to %APPDATA%\\MultiZip\\icons.log (this session only) so a missing icon can be diagnosed."""
        self.fails = getattr(self, "fails", 0) + 1
        if self.fails > 40:
            return
        try:
            with open(os.path.join(os.path.dirname(settings_path()), "icons.log"), "w" if self.fails == 1 else "a",
                      encoding="utf-8") as f:
                f.write(f"{key}: {why}\n")
        except OSError:
            pass

    def load(self, name, real, attr, n=16):
        """Icon as a PIL image of exactly n x n. Each size is fetched at its NATIVE size so it stays sharp:
        16 = small icon, 32 = large icon, 48 / 256 = the shell's extra-large / jumbo icon lists."""
        import ctypes
        from ctypes import wintypes
        u32, g32, sh = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.shell32
        vp = ctypes.c_void_p

        class SHFILEINFOW(ctypes.Structure):
            _fields_ = [("hIcon", vp), ("iIcon", ctypes.c_int), ("dwAttributes", wintypes.DWORD),
                        ("szDisplayName", ctypes.c_wchar * 260), ("szTypeName", ctypes.c_wchar * 80)]

        class BMIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                        ("hbmMask", vp), ("hbmColor", vp)]

        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                        ("bmWidthBytes", ctypes.c_long), ("bmPlanes", wintypes.WORD),
                        ("bmBitsPixel", wintypes.WORD), ("bmBits", vp)]

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]
        sh.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(SHFILEINFOW),
                                      wintypes.UINT, wintypes.UINT]
        sh.SHGetFileInfoW.restype = vp
        sh.SHGetImageList.argtypes = [ctypes.c_int, ctypes.POINTER(GUID), ctypes.POINTER(vp)]
        sh.SHGetImageList.restype = ctypes.c_long
        u32.GetDC.argtypes, u32.GetDC.restype = [vp], vp
        u32.ReleaseDC.argtypes = [vp, vp]
        u32.DestroyIcon.argtypes = [vp]
        u32.DrawIconEx.argtypes = [vp, ctypes.c_int, ctypes.c_int, vp, ctypes.c_int, ctypes.c_int,
                                   wintypes.UINT, vp, wintypes.UINT]
        g32.CreateCompatibleDC.argtypes, g32.CreateCompatibleDC.restype = [vp], vp
        g32.CreateDIBSection.argtypes = [vp, ctypes.POINTER(BMIH), wintypes.UINT, ctypes.POINTER(vp), vp, wintypes.DWORD]
        g32.CreateDIBSection.restype = vp
        g32.SelectObject.argtypes, g32.SelectObject.restype = [vp, vp], vp
        g32.DeleteObject.argtypes = [vp]
        g32.DeleteDC.argtypes = [vp]
        u32.GetIconInfo.argtypes = [vp, ctypes.POINTER(ICONINFO)]
        g32.GetObjectW.argtypes = [vp, ctypes.c_int, ctypes.c_void_p]

        def real_size(hic):
            """The icon's true pixel size. (The size Windows hands out depends on the screen scaling, so
            drawing it at an assumed size would squash or stretch it - that is what made it look pixelated.)"""
            try:
                ii, bm = ICONINFO(), BITMAP()
                if not u32.GetIconInfo(hic, ctypes.byref(ii)):
                    return 0
                try:
                    src = ii.hbmColor or ii.hbmMask
                    g32.GetObjectW(src, ctypes.sizeof(bm), ctypes.byref(bm))
                    w = int(bm.bmWidth)
                    return w if 8 <= w <= 512 else 0
                finally:
                    for h in (ii.hbmColor, ii.hbmMask):
                        if h:
                            g32.DeleteObject(h)
            except Exception:
                return 0

        def render(hic, px_hint):
            """Draw the icon at its own size on black, then on white, and rebuild the transparency."""
            px = real_size(hic) or px_hint
            screen = u32.GetDC(None)
            mdc = g32.CreateCompatibleDC(screen)
            bits = vp()
            hbm = g32.CreateDIBSection(mdc, ctypes.byref(BMIH(40, px, -px, 1, 32, 0, 0, 0, 0, 0, 0)), 0,
                                       ctypes.byref(bits), None, 0)
            shots = []
            try:
                old = g32.SelectObject(mdc, hbm)
                for fill in (0x00, 0xFF):
                    ctypes.memset(bits, fill, px * px * 4)
                    u32.DrawIconEx(mdc, 0, 0, hic, px, px, 0, None, 3)           # DI_NORMAL, no scaling
                    shots.append(ctypes.string_at(bits, px * px * 4))
                g32.SelectObject(mdc, old)
            finally:
                g32.DeleteObject(hbm)
                g32.DeleteDC(mdc)
                u32.ReleaseDC(None, screen)
            return Image.frombytes("RGBA", (px, px), icon_rgba(shots[0], shots[1], px))

        info = SHFILEINFOW()
        use_attr = 0 if real else 0x10                                           # USEFILEATTRIBUTES
        if n > 32:
            try:
                # system icon list: 48 px (extra large) or 256 px (jumbo); also fetches the shortcut arrow overlay
                flags = 0x100 | 0x4000 | 0x40 | use_attr                             # ICON | SYSICONINDEX | OVERLAYINDEX
                if sh.SHGetFileInfoW(name, attr, ctypes.byref(info), ctypes.sizeof(info), flags):
                    if info.hIcon:
                        u32.DestroyIcon(info.hIcon)
                    idx, overlay = info.iIcon & 0xFFFFFF, (info.iIcon >> 24) & 0xFF
                    iid = GUID(0x46EB5926, 0x582E, 0x4017, (ctypes.c_ubyte * 8)(0x9F, 0xDF, 0xE8, 0x99, 0x8D, 0xAA, 0x09, 0x50))
                    himl = vp()
                    if sh.SHGetImageList(2 if n <= 48 else 4, ctypes.byref(iid), ctypes.byref(himl)) == 0 and himl:
                        vt = ctypes.cast(himl, ctypes.POINTER(ctypes.POINTER(vp)))[0]
                        get_icon = ctypes.WINFUNCTYPE(ctypes.c_long, vp, ctypes.c_int, wintypes.UINT,
                                                      ctypes.POINTER(vp))(vt[10])
                        get_size = ctypes.WINFUNCTYPE(ctypes.c_long, vp, ctypes.POINTER(ctypes.c_int),
                                                      ctypes.POINTER(ctypes.c_int))(vt[16])
                        cx, cy, hic = ctypes.c_int(), ctypes.c_int(), vp()
                        get_size(himl, ctypes.byref(cx), ctypes.byref(cy))           # a hint only; real_size() decides
                        if get_icon(himl, idx, 1 | (overlay << 8), ctypes.byref(hic)) == 0 and hic:   # ILD_TRANSPARENT
                            try:
                                im = render(hic, cx.value or (48 if n <= 48 else 256))
                            finally:
                                u32.DestroyIcon(hic)
                            return im if im.size == (n, n) else im.resize((n, n), RESAMPLE)
            except Exception:
                self.log(("list", n), traceback.format_exc()[-400:])
            n_native = 32                                                        # list unavailable: plain large icon
        else:
            n_native = 16 if n <= 16 else 32
        flags = 0x100 | (0x1 if n_native == 16 else 0) | (0x20 if real and not attr & 0x10 else 0) | use_attr
        if not sh.SHGetFileInfoW(name, attr, ctypes.byref(info), ctypes.sizeof(info), flags) or not info.hIcon:
            return None
        try:
            im = render(info.hIcon, n_native)
        finally:
            u32.DestroyIcon(info.hIcon)
        return im if im.size == (n, n) else im.resize((n, n), RESAMPLE)


ICONS = None


def icon_for(path, isdir=False, size=16):
    global ICONS
    if ICONS is None:
        ICONS = IconCache()
    return ICONS.get(path, isdir, size)


def short_path(p, n=72):
    return p if len(p) <= n else p[:20] + " ... " + p[-(n - 25):]


SCAN_ALL = "All drives  (scan everything)"


class ToolWindow(tk.Toplevel):
    """Base for the windows that run a worker thread: closing stops it."""

    def close(self):
        self.alive = False
        self.stop.set()
        self.destroy()


class DiskUsageDialog(ToolWindow):
    """Shows what is using the space on a drive or in a folder."""
    MAX_KIDS = 400

    def __init__(self, app, start=""):
        super().__init__(app)
        self.app = app
        self.configure(bg=PAL["bg"])
        self.title("Disk usage analyser")
        self.geometry("940x660")
        self.minsize(740, 500)
        self.q, self.stop, self.worker, self.alive = queue.Queue(), threading.Event(), None, True
        self.nodes, self.paths, self.fpaths, self.data = {}, {}, {}, None
        self.loc, self.drive_map = tk.StringVar(value=start), {}
        self.protocol("WM_DELETE_WINDOW", self.close)

        top = ttk.Frame(self, padding=(12, 12, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Look at:").pack(side="left")
        self.box = ttk.Combobox(top, textvariable=self.loc, postcommand=self.fill_drives)
        self.box.pack(side="left", fill="x", expand=True, padx=6)
        self.box.bind("<<ComboboxSelected>>", self.on_pick)
        self.box.bind("<Return>", lambda _: self.scan())
        Tip(self.box, "Pick a drive from the list, or type / paste a folder path.")
        ttk.Button(top, text="Choose folder...", command=self.pick_folder).pack(side="left")
        self.go = ttk.Button(top, text="Scan", style="Accent.TButton", command=self.scan)
        self.go.pack(side="left", padx=(6, 0))
        self.allb = ttk.Button(top, text="Scan all drives", command=self.scan_all)
        self.allb.pack(side="left", padx=(6, 0))
        Tip(self.allb, "Scan every internal and removable drive in one go (network drives are skipped).")
        self.stopb = ttk.Button(top, text="Stop", command=self.stop.set, state="disabled")
        self.stopb.pack(side="left", padx=(6, 0))

        self.bar = tk.Canvas(self, height=28, highlightthickness=0, bg=PAL["bg"])
        self.bar.pack(fill="x", padx=12, pady=(6, 2))
        self.bar.bind("<Configure>", lambda _: self.draw_bar())
        self.msg = ttk.Label(self, text="Press Scan to see what is taking up space.", anchor="w")
        self.msg.pack(fill="x", padx=12, pady=(0, 4))

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=4)
        f1, f2, f3 = ttk.Frame(self.tabs, padding=4), ttk.Frame(self.tabs, padding=4), ttk.Frame(self.tabs, padding=4)
        self.tabs.add(f1, text="  Folders  ")
        self.tabs.add(f2, text="  Largest files  ")
        self.tabs.add(f3, text="  File types  ")
        self.tree = make_tree(f1, "Folder", [("size", "Size", 100, "e"), ("share", "Share of parent folder", 170, "w"),
                                             ("files", "Files", 90, "e")])
        self.ftree = make_tree(f2, "File", [("size", "Size", 100, "e"), ("mod", "Modified", 130, "w"),
                                            ("dir", "Folder", 300, "w")])
        self.etree = make_tree(f3, "Type", [("size", "Size", 100, "e"), ("share", "Share of everything scanned", 190, "w"),
                                            ("count", "Files", 90, "e")], selectmode="browse")
        self.tree.bind("<<TreeviewOpen>>", self.on_open)

        bt = ttk.Frame(self, padding=(12, 6, 12, 12))
        bt.pack(fill="x")
        ttk.Button(bt, text="Show in Explorer", command=self.show_in_explorer).pack(side="left")
        ttk.Button(bt, text="Open in Browse", command=self.open_in_browse).pack(side="left", padx=6)
        self.binb = ttk.Button(bt, text="Move to Recycle Bin", command=self.to_bin)
        self.binb.pack(side="left")
        ttk.Button(bt, text="Close", command=self.close).pack(side="right")
        ttk.Label(bt, text="Sizes are file sizes, not the space reserved on disk.",
                  foreground=PAL["fg"]).pack(side="right", padx=12)
        self.after(100, self.poll)
        self.draw_bar()

    # ---- location
    def fill_drives(self):
        items = [(SCAN_ALL, SCAN_ALL)] + [(t, p) for t, p in list_drives() if p]
        self.drive_map = dict(items)
        self.box.configure(values=[t for t, _ in items])

    def on_pick(self, _=None):
        p = self.drive_map.get(self.loc.get())
        if p:
            self.loc.set(p)
            self.draw_bar()

    def pick_folder(self):
        d = filedialog.askdirectory(parent=self, title="Choose a folder to analyse")
        if d:
            self.loc.set(os.path.normpath(d))
            self.draw_bar()

    def chosen(self):
        t = self.loc.get().strip().strip('"')
        if t == SCAN_ALL:
            return SCAN_ALL
        return os.path.abspath(self.drive_map.get(t, t)) if t else ""

    def draw_bar(self):
        """How full the drive that holds the chosen location is."""
        c = self.bar
        c.delete("all")
        w = max(50, c.winfo_width())
        p = self.chosen()
        try:
            if p == SCAN_ALL:
                parts = [shutil.disk_usage(r) for r in local_drive_roots()]
                u = types.SimpleNamespace(used=sum(x.used for x in parts), free=sum(x.free for x in parts),
                                          total=sum(x.total for x in parts))
            else:
                u = shutil.disk_usage(p)
        except (OSError, ValueError):
            return
        c.create_rectangle(0, 2, w - 1, 26, fill=PAL["lbg"], outline=PAL["border"])
        frac = u.used / u.total if u.total else 0
        c.create_rectangle(1, 3, max(2, int((w - 2) * frac)), 25, fill=PAL["accent"] if frac < .9 else "#d9534f",
                           outline="")
        c.create_text(10, 14, anchor="w", fill=PAL["accent_fg"] if frac > .12 else PAL["lfg"], font=fnt(9, True),
                      text=f"{human(u.used)} used")
        c.create_text(w - 10, 14, anchor="e", fill=PAL["lfg"], font=fnt(9),
                      text=f"{human(u.free)} free of {human(u.total)}")

    # ---- scanning
    def scan_all(self):
        if not (self.worker and self.worker.is_alive()):
            self.loc.set(SCAN_ALL)
            self.scan()

    def scan(self):
        if self.worker and self.worker.is_alive():
            return
        p = self.chosen()
        roots = local_drive_roots() if p == SCAN_ALL else [p]
        if not roots or not p or (p != SCAN_ALL and not os.path.isdir(p)):
            return messagebox.showinfo(APP, "Pick a drive or folder to scan first.", parent=self)
        self.stop.clear()
        self.go.config(state="disabled")
        self.stopb.config(state="normal")
        for t in (self.tree, self.ftree, self.etree):
            t.delete(*t.get_children())
        self.nodes.clear()
        self.paths.clear()
        self.fpaths.clear()
        self.draw_bar()
        self.msg.config(text="Scanning...")
        self.allb.config(state="disabled")
        self.worker = threading.Thread(target=self.work, args=(roots,), daemon=True)
        self.worker.start()

    def work(self, roots):
        try:
            base, results = dict(files=0, bytes=0, folders=0, skipped=0), []
            for root in roots:
                d = scan_usage(root, self.stop.is_set,
                               lambda st, p: self.q.put(("prog", {k: base[k] + st[k] for k in base}, p)))
                results.append(d)
                for k in base:
                    base[k] += d["stats"][k]
            self.q.put(("done", results[0] if len(results) == 1 else merge_usage(results)))
        except Cancelled:
            self.q.put(("cancel",))
        except Exception as e:
            self.q.put(("err", str(e) or e.__class__.__name__))

    def poll(self):
        if not self.alive:
            return
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "prog":
                    st = m[1]
                    self.msg.config(text=f"Scanning...  {st['files']:,} files  -  {human(st['bytes'])}  -  "
                                         f"{short_path(m[2])}")
                else:
                    self.go.config(state="normal")
                    self.allb.config(state="normal")
                    self.stopb.config(state="disabled")
                    if m[0] == "done":
                        self.show_result(m[1])
                    elif m[0] == "cancel":
                        self.msg.config(text="Scan stopped.")
                    else:
                        self.msg.config(text="The scan failed: " + m[1])
        except queue.Empty:
            pass
        self.after(100, self.poll)

    # ---- results
    def show_result(self, data):
        self.data = data
        root, st = data["root"], data["stats"]
        text = f"{st['files']:,} files in {st['folders']:,} folders  -  {human(st['bytes'])}"
        if st["skipped"]:
            text += f"   ({st['skipped']:,} folders or files could not be read)"
        self.msg.config(text=text)
        rid = self.add_node("", root, 100.0)
        self.fill_children(rid)
        self.tree.item(rid, open=True)
        for i, (size, path, mod) in enumerate(data["top"]):
            iid = f"f{i}"
            self.fpaths[iid] = path
            self.ftree.insert("", "end", iid=iid, text=os.path.basename(path), image=icon_for(path) or "",
                              values=(human(size), datetime.fromtimestamp(mod).strftime("%Y-%m-%d %H:%M"),
                                      os.path.dirname(path)))
        total = max(1, st["bytes"])
        for x, (size, n) in sorted(data["ext"].items(), key=lambda kv: -kv[1][0])[:300]:
            self.etree.insert("", "end", text=x, image=icon_for("x" + (x if x.startswith(".") else "")) or "",
                              values=(human(size), share_bar(size * 100 / total), f"{n:,}"))
        self.draw_bar()

    def add_node(self, parent, node, pct):
        iid = self.tree.insert(parent, "end", text=node.name or node.path,
                               image=(icon_for(node.path, True) if node.path else None) or "",
                               values=(human(node.size), share_bar(pct), f"{node.files:,}"))
        self.nodes[iid] = node
        self.paths[iid] = node.path
        if node.dirs:
            self.tree.insert(iid, "end", iid=iid + "_pending", text="")
        return iid

    def fill_children(self, iid):
        node = self.nodes[iid]
        pend = iid + "_pending"
        if not self.tree.exists(pend):
            return
        self.tree.delete(pend)
        whole = max(1, node.size)
        kids = sorted(node.dirs, key=lambda d: -d.size)
        for k in kids[:self.MAX_KIDS]:
            self.add_node(iid, k, k.size * 100 / whole)
        if len(kids) > self.MAX_KIDS:
            rest = kids[self.MAX_KIDS:]
            self.tree.insert(iid, "end", text=f"({len(rest):,} more smaller folders)",
                             values=(human(sum(k.size for k in rest)), "", f"{sum(k.files for k in rest):,}"))
        if node.own_files:
            self.tree.insert(iid, "end", text="(files directly in this folder)",
                             values=(human(node.own), share_bar(node.own * 100 / whole), f"{node.own_files:,}"))

    def on_open(self, _=None):
        iid = self.tree.focus()
        if iid in self.nodes:
            self.fill_children(iid)

    # ---- actions on the selection
    def targets(self):
        if self.tabs.index(self.tabs.select()) == 0:
            return [(i, self.paths[i]) for i in self.tree.selection() if self.paths.get(i)]
        if self.tabs.index(self.tabs.select()) == 1:
            return [(i, self.fpaths[i]) for i in self.ftree.selection() if i in self.fpaths]
        return []

    def need_selection(self):
        t = self.targets()
        if not t:
            messagebox.showinfo(APP, "Select a folder or file in the Folders or Largest files tab first.",
                                parent=self)
        return t

    def show_in_explorer(self):
        t = self.need_selection()
        if t:
            p = t[0][1]
            open_folder(p) if os.path.isdir(p) else reveal_file(p)

    def open_in_browse(self):
        t = self.need_selection()
        if t:
            p = t[0][1]
            self.app.goto(p if os.path.isdir(p) else os.path.dirname(p))
            self.app.lift()
            self.close()

    def to_bin(self):
        t = self.need_selection()
        if not t:
            return
        for _i, p in t:
            why = protected_reason(p)
            if why:
                return messagebox.showinfo(APP, f"{p}\n\n{why}\nMultiZip won't remove it.", parent=self)
        if os.name != "nt":
            return messagebox.showinfo(APP, "The Recycle Bin is only available on Windows.", parent=self)
        names = "\n".join("  " + short_path(p, 80) for _i, p in t[:8]) + (f"\n  ... and {len(t) - 8} more" if len(t) > 8 else "")
        if not messagebox.askyesno(APP, f"Move {len(t)} item(s) to the Recycle Bin?\n\n{names}\n\n"
                                        "You can restore them from the Recycle Bin.", parent=self):
            return
        if recycle([p for _i, p in t]):
            tv = self.tree if self.tabs.index(self.tabs.select()) == 0 else self.ftree
            for i, _p in t:
                if tv.exists(i):
                    tv.delete(i)
            self.msg.config(text=f"Moved {len(t)} item(s) to the Recycle Bin.  Scan again to update the totals.")
        else:
            messagebox.showerror(APP, "Windows could not move them (they may be in use).", parent=self)

class CleanupDialog(ToolWindow):
    """Finds junk that is safe to remove, shows how much, and cleans only what you tick."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.configure(bg=PAL["bg"])
        self.title("Clean up assistant")
        self.geometry("820x600")
        self.minsize(680, 480)
        self.cats = cleanup_categories()
        self.by_id = {c["id"]: c for c in self.cats}
        self.checked = {c["id"]: bool(c["default"]) for c in self.cats}
        self.res, self.note = {}, ""                        # res: id -> (bytes, items) once scanned
        self.q, self.stop, self.worker, self.alive, self.busy = queue.Queue(), threading.Event(), None, True, False
        self.protocol("WM_DELETE_WINDOW", self.close)

        ttk.Label(self, text="Clean up assistant", font=fnt(14, True)).pack(anchor="w", padx=14, pady=(14, 0))
        ttk.Label(self, wraplength=760, justify="left",
                  text="Tick what you want to clean and press the button. Only the places listed here are touched, "
                       "and nothing is deleted until you confirm.").pack(anchor="w", padx=14, pady=(2, 8))
        wrap = ttk.Frame(self, padding=(14, 0))
        wrap.pack(fill="both", expand=True)
        self.tv = ttk.Treeview(wrap, columns=("do", "name", "size", "items"), show="headings",
                               selectmode="browse", height=9)
        for cid, head, w, a in (("do", "Clean", 60, "center"), ("name", "What", 380, "w"),
                                ("size", "Can free", 110, "e"), ("items", "Items", 90, "e")):
            self.tv.heading(cid, text=head, anchor=a)
            self.tv.column(cid, width=w, anchor=a, stretch=(cid == "name"))
        self.tv.pack(fill="both", expand=True)
        for c in self.cats:
            self.tv.insert("", "end", iid=c["id"], values=self.row(c))
        self.tv.bind("<Button-1>", self.on_click)
        self.tv.bind("<space>", self.on_space)
        self.tv.bind("<<TreeviewSelect>>", self.on_select)
        self.desc = ttk.Label(self, wraplength=780, justify="left", text="", padding=(14, 8))
        self.desc.pack(fill="x")
        self.total = ttk.Label(self, text="", font=fnt(11, True), padding=(14, 0))
        self.total.pack(fill="x")
        self.prog = ttk.Progressbar(self, mode="indeterminate")
        self.prog.pack(fill="x", padx=14, pady=(8, 0))
        self.msg = ttk.Label(self, text="Looking for things to clean...", anchor="w", wraplength=780, padding=(14, 4))
        self.msg.pack(fill="x")
        bt = ttk.Frame(self, padding=(14, 4, 14, 14))
        bt.pack(fill="x")
        self.cleanb = ttk.Button(bt, text="Clean up selected", style="Accent.TButton", command=self.clean,
                                 state="disabled")
        self.cleanb.pack(side="left")
        self.rescanb = ttk.Button(bt, text="Scan again", command=self.scan, state="disabled")
        self.rescanb.pack(side="left", padx=6)
        self.stopb = ttk.Button(bt, text="Stop", command=self.stop.set, state="disabled")
        self.stopb.pack(side="left")
        ttk.Button(bt, text="Close", command=self.close).pack(side="right")
        self.after(100, self.poll)
        if self.cats:
            self.tv.selection_set(self.cats[0]["id"])
        self.scan()

    # ---- rows
    def row(self, c):
        r = self.res.get(c["id"])
        size = "scanning..." if r is None else human(r[0])
        items = "" if r is None else f"{r[1]:,}"
        return ("☑" if self.checked[c["id"]] else "☐", c["name"], size, items)

    def refresh_row(self, cid):
        if self.tv.exists(cid):
            self.tv.item(cid, values=self.row(self.by_id[cid]))
        self.update_total()

    def update_total(self):
        n = sum(r[0] for cid, r in self.res.items() if self.checked.get(cid))
        self.total.config(text=f"Selected: {human(n)} can be freed")

    def on_click(self, e):
        if self.busy or self.tv.identify_region(e.x, e.y) != "cell" or self.tv.identify_column(e.x) != "#1":
            return
        cid = self.tv.identify_row(e.y)
        if cid:
            self.toggle(cid)
            return "break"

    def on_space(self, _):
        sel = self.tv.selection()
        if sel and not self.busy:
            self.toggle(sel[0])
        return "break"

    def toggle(self, cid):
        self.checked[cid] = not self.checked[cid]
        self.refresh_row(cid)

    def on_select(self, _=None):
        sel = self.tv.selection()
        self.desc.config(text=self.by_id[sel[0]]["desc"] if sel else "")

    # ---- scanning
    def set_busy(self, busy, stoppable=False):
        self.busy = busy
        self.stopb.config(state="normal" if busy and stoppable else "disabled")
        self.rescanb.config(state="disabled" if busy else "normal")
        self.cleanb.config(state="disabled" if busy else "normal")
        if busy:
            self.prog.start(12)
        else:
            self.prog.stop()

    def scan(self):
        if self.busy:
            return
        self.res.clear()
        for c in self.cats:
            self.tv.item(c["id"], values=self.row(c))
        self.stop.clear()
        self.set_busy(True, True)
        self.msg.config(text="Looking for things to clean...")
        self.worker = threading.Thread(target=self.scan_work, daemon=True)
        self.worker.start()

    def scan_work(self):
        try:
            for c in self.cats:
                try:
                    self.q.put(("scan", c["id"], scan_category(c, self.stop.is_set)))
                except Cancelled:
                    raise
                except Exception:
                    self.q.put(("scan", c["id"], (0, 0)))
            self.q.put(("scanned", False))
        except Cancelled:
            self.q.put(("scanned", True))

    def clean(self):
        todo = [c for c in self.cats if self.checked[c["id"]] and self.res.get(c["id"], (0, 0))[1] > 0]
        if not todo:
            return messagebox.showinfo(APP, "Nothing is ticked, or there is nothing to clean there.", parent=self)
        todo.sort(key=lambda c: c["mode"] != "recyclebin")          # empty the bin first, then recycle downloads
        perm = [c["name"] for c in todo if c["mode"] != "recycle"]
        rec = [c["name"] for c in todo if c["mode"] == "recycle"]
        text = ""
        if perm:
            text += "These will be deleted for good:\n" + "\n".join("  - " + n for n in perm) + "\n\n"
        if rec:
            text += "These will be moved to the Recycle Bin (you can restore them):\n" + \
                    "\n".join("  - " + n for n in rec) + "\n\n"
        total = sum(self.res[c["id"]][0] for c in todo)
        if not messagebox.askyesno(APP, text + f"About {human(total)} will be freed. Continue?", parent=self):
            return
        self.stop.clear()
        self.set_busy(True, True)
        self.msg.config(text="Cleaning...")
        self.worker = threading.Thread(target=self.clean_work, args=(todo,), daemon=True)
        self.worker.start()

    def clean_work(self, todo):
        freed = done = skipped = 0
        try:
            for c in todo:
                self.q.put(("doing", c["name"]))
                try:
                    f, d, s = clean_category(c, self.stop.is_set)
                except Cancelled:
                    raise
                except Exception:
                    f = d = 0
                    s = 1
                freed, done, skipped = freed + f, done + d, skipped + s
        except Cancelled:
            pass
        self.q.put(("cleaned", freed, done, skipped))

    def poll(self):
        if not self.alive:
            return
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "scan":
                    self.res[m[1]] = m[2]
                    self.refresh_row(m[1])
                elif m[0] == "scanned":
                    self.set_busy(False)
                    n = sum(r[0] for r in self.res.values())
                    found = ("Scan stopped." if m[1] else
                             (f"Found {human(n)} that can be cleaned." if n else "Nothing to clean right now."))
                    self.msg.config(text=(self.note + "\n" if self.note else "") + found)
                    self.note = ""
                elif m[0] == "doing":
                    self.msg.config(text="Cleaning:  " + m[1])
                elif m[0] == "cleaned":
                    self.set_busy(False)
                    text = f"Done.  Freed {human(m[1])} ({m[2]:,} items)."
                    if m[3]:
                        text += f"  {m[3]:,} items were skipped because they are in use or need administrator rights."
                    self.note = text
                    self.scan()
        except queue.Empty:
            pass
        self.after(100, self.poll)


# ===================================================== bootable USB maker
# Everything here is built so a wrong click cannot reach the wrong disk: only USB / SD / removable disks are
# ever listed, never a system or boot disk, the choice is re-checked right before anything is written, and the
# user must type ERASE first.
FAT32_MAX_FILE = 4 * 1024 ** 3 - 1
FAT32_PART_MB = 32000                               # Windows will not format FAT32 volumes larger than 32 GB
BIG_DISK = 256 * 1000 ** 3                          # bigger "USB" disks are probably external hard drives
CHUNK = 4 * 1024 * 1024
BOOT_NONE = "Not bootable  (just format the drive)"
BOOT_WIN = "Windows installer ISO  (copy the files)"
BOOT_DD = "Disk image / Linux ISO  (DD mode)"
LABEL_MAX = {"FAT32": 11, "exFAT": 15, "NTFS": 32}


def is_admin():
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def restart_as_admin():
    """Start this program again with administrator rights (Windows asks the user to allow it)."""
    import ctypes
    frozen = getattr(sys, "frozen", False)
    params = "--usb-tool" if frozen else f'"{os.path.abspath(sys.argv[0])}" --usb-tool'
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1) > 32


def clean_label(label, fs):
    bad = '"*/:<>?\\|' + ("" if fs != "FAT32" else ".,;=+[]")
    out = "".join(ch for ch in (label or "") if ch not in bad and ch.isprintable()).strip()
    out = out.upper() if fs == "FAT32" else out
    return out[:LABEL_MAX.get(fs, 11)] or "USB"


def ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def ps_run(script, timeout=120):
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", script], capture_output=True, text=True, timeout=timeout, **_flags())
    return p.returncode, p.stdout or "", p.stderr or ""


def parse_disks(text, removable=frozenset(), system_letter="C"):
    """Turn Get-Disk's JSON into the list of disks that are safe to offer. Anything uncertain is left out."""
    text = (text or "").strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data:
        try:
            n, size = int(d["Number"]), int(d.get("Size") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        letters = [x.upper() for x in str(d.get("Letters") or "").split(",") if x.strip()]
        bus = str(d.get("Bus") or "").upper()
        if size <= 0 or d.get("IsSystem") is not False or d.get("IsBoot") is not False:
            continue                                         # empty card slot, or not clearly a non-system disk
        if system_letter.upper() in letters:
            continue
        if bus not in ("USB", "SD", "MMC") and not any(x in removable for x in letters):
            continue
        out.append(dict(number=n, name=str(d.get("Name") or "USB drive").strip(), size=size, bus=bus,
                        letters=letters, big=size > BIG_DISK, offline=bool(d.get("Offline")),
                        readonly=bool(d.get("ReadOnly"))))
    return sorted(out, key=lambda x: x["number"])


def describe_disk(d):
    where = ", ".join(x + ":" for x in d["letters"]) if d["letters"] else "no drive letter"
    return f"Disk {d['number']}  -  {d['name']}  -  {human(d['size'])}  -  {where}"


LIST_DISKS = ("Get-Disk | ForEach-Object { $n = $_.Number; [pscustomobject]@{ Number = $n; Name = $_.FriendlyName; "
              "Size = [int64]$_.Size; Bus = [string]$_.BusType; IsSystem = $_.IsSystem; IsBoot = $_.IsBoot; "
              "Offline = $_.IsOffline; ReadOnly = $_.IsReadOnly; Letters = ((Get-Partition -DiskNumber $n "
              "-ErrorAction SilentlyContinue | Where-Object DriveLetter | ForEach-Object { [string]$_.DriveLetter }) "
              "-join ',') } } | ConvertTo-Json -Compress")


def list_usb_disks(include_big=False):
    """-> (disks, error text)."""
    if os.name != "nt":
        return [], "Making a bootable USB drive only works on Windows."
    try:
        rc, out, err = ps_run(LIST_DISKS, 60)
        if rc != 0:
            return [], (err or out).strip()[:300] or "Windows could not list the disks."
        import ctypes
        k32 = ctypes.windll.kernel32
        removable = {chr(65 + i) for i in range(26) if k32.GetDriveTypeW(f"{chr(65 + i)}:\\") == 2}
        disks = parse_disks(out, removable, os.environ.get("SystemDrive", "C:")[:1])
        return [d for d in disks if include_big or not d["big"]], ""
    except Exception as e:
        return [], f"Could not list the disks: {e}"


def script_format(num, scheme, fs, label, active, size_mb=None):
    """The diskpart script that wipes the disk and makes one formatted partition."""
    fsname = {"FAT32": "fat32", "exFAT": "exfat", "NTFS": "ntfs"}[fs]
    lines = [f"select disk {int(num)}", "clean", "convert gpt" if scheme == "GPT" else "convert mbr",
             "create partition primary" + (f" size={int(size_mb)}" if size_mb else ""),
             f'format fs={fsname} label="{clean_label(label, fs)}" quick']
    if active and scheme != "GPT":
        lines.append("active")
    lines += ["assign noerr", "exit"]                         # noerr: Windows may already have given it a letter
    return "\r\n".join(lines) + "\r\n"


def script_clean(num):
    return f"select disk {int(num)}\r\nclean\r\nexit\r\n"


def run_diskpart(script):
    """Run a diskpart script; raises RuntimeError if diskpart reports a failure (it stops at the first error)."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="multizip_dp_")
    try:
        with os.fdopen(fd, "w", encoding="ascii", errors="replace") as f:
            f.write(script)
        p = subprocess.run(["diskpart", "/s", path], capture_output=True, text=True, timeout=900, **_flags())
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            raise RuntimeError("Windows could not prepare the drive. Close any files or windows that use it "
                               "and try again.\n\n" + out.strip()[-300:])
        return out
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def disk_letters(num):
    rc, out, _e = ps_run(f"Get-Partition -DiskNumber {int(num)} -ErrorAction SilentlyContinue | "
                         "Where-Object DriveLetter | ForEach-Object { [string]$_.DriveLetter }", 60)
    return [x.strip().upper() for x in out.split() if x.strip()] if rc == 0 else []


def volume_fs(root):
    try:
        import ctypes
        name = ctypes.create_unicode_buffer(64)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, name, 64)
        return name.value if ok else ""
    except Exception:
        return ""


def wait_for_volume(num, fs, cancelled, tries=40):
    """Wait until the freshly formatted partition shows up with the right file system. -> 'E:\\'"""
    for _ in range(tries):
        if cancelled():
            raise Cancelled
        for L in disk_letters(num):
            root = L + ":\\"
            if os.path.isdir(root) and volume_fs(root).lower() == fs.lower():
                return root
        time.sleep(1)
    raise RuntimeError("The drive was prepared, but Windows did not show the new volume in time. "
                       "Unplug the drive, plug it in again and check it in Explorer.")


def iso_info(path):
    """Quick facts about an image file: size, looks like an ISO, and whether it can be written as-is (hybrid)."""
    info = dict(size=os.path.getsize(path), iso=False, hybrid=False)
    with open(path, "rb") as f:
        head = f.read(512)
        f.seek(0x8000)
        info["iso"] = f.read(6)[1:6] == b"CD001"
    info["hybrid"] = len(head) == 512 and head[510:512] == b"\x55\xaa"
    return info


def is_windows_media(root):
    src = os.path.join(root, "sources")
    return os.path.isdir(src) and any(os.path.isfile(os.path.join(src, n)) for n in
                                      ("install.wim", "install.esd", "install.swm", "boot.wim"))


def mount_iso(path):
    rc, out, err = ps_run(f"$i = Mount-DiskImage -ImagePath {ps_quote(path)} -PassThru; "
                          "($i | Get-Volume).DriveLetter", 180)
    letter = out.strip()[:1]
    if rc != 0 or not letter.isalpha():
        raise RuntimeError("Windows could not open the ISO file: " + ((err or out).strip()[:300] or "unknown error"))
    return letter.upper() + ":\\"


def unmount_iso(path):
    try:
        ps_run(f"Dismount-DiskImage -ImagePath {ps_quote(path)}", 60)
    except Exception:
        pass


def copy_tree(src, dst, progress, cancelled, fat32=True):
    """Copy a folder tree. With FAT32, files over 4 GB cannot be stored: they are returned instead."""
    files, total = [], 0
    for base, _dirs, names in os.walk(src):
        for n in names:
            p = os.path.join(base, n)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            files.append((p, os.path.relpath(p, src), sz))
            total += sz
    big, done = [], 0
    for p, rel, sz in files:
        if cancelled():
            raise Cancelled
        if fat32 and sz > FAT32_MAX_FILE:
            big.append((p, rel, sz))
            continue
        out = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(p, "rb") as a, open(out, "wb") as b:
            while True:
                if cancelled():
                    raise Cancelled
                buf = a.read(CHUNK)
                if not buf:
                    break
                b.write(buf)
                done += len(buf)
                progress(done, total, rel)
    return big


RESERVED_USERS = {"administrator", "guest", "system", "defaultaccount", "wdagutilityaccount", "default", "public",
                  "all users", "network service", "local service"}


def valid_username(name):
    """Rules Windows applies to local account names. -> '' when fine, else what is wrong."""
    n = name or ""
    if not n.strip():
        return "Type a user name."
    if len(n) > 20:
        return "The name can be at most 20 characters."
    if any(ch in '"/\\[]:;|=,+*?<>@' or ord(ch) < 32 for ch in n):
        return 'The name cannot contain any of  " / \\ [ ] : ; | = , + * ? < > @'
    if n != n.strip() or n.endswith("."):
        return "The name cannot start or end with a space, or end with a full stop."
    if n.lower() in RESERVED_USERS:
        return f"'{n}' is a name Windows already uses. Pick another."
    return ""


def build_unattend(user, password=""):
    """autounattend.xml: Windows Setup reads it from the USB drive, makes the local account and
    hides the online (Microsoft) account screens."""
    from xml.sax.saxutils import escape
    pw = (f"<Password><Value>{escape(password)}</Value><PlainText>true</PlainText></Password>"
          if password else "")
    parts = []
    for arch in ("amd64", "arm64", "x86"):                 # Setup only uses the one that matches the media
        parts.append(f'''    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="{arch}" \
publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" \
xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" \
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <OOBE>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{escape(user)}</Name>
            <DisplayName>{escape(user)}</DisplayName>
            <Group>Administrators</Group>
            {pw}
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
    </component>''')
    return ('<?xml version="1.0" encoding="utf-8"?>\n<unattend xmlns="urn:schemas-microsoft-com:unattend">\n'
            '  <settings pass="oobeSystem">\n' + "\n".join(parts) + "\n  </settings>\n</unattend>\n")


def split_wim(wim, swm):
    os.makedirs(os.path.dirname(swm), exist_ok=True)
    p = subprocess.run(["dism", "/Split-Image", f"/ImageFile:{wim}", f"/SWMFile:{swm}", "/FileSize:3800"],
                       capture_output=True, text=True, timeout=7200, **_flags())
    if p.returncode != 0:
        raise RuntimeError("Windows could not split install.wim so it fits on FAT32:\n" + (p.stdout or p.stderr)[-400:])


def write_image(src, dst, dst_size, progress, cancelled, verify=True):
    """Copy an image byte for byte onto a (physical) drive, in aligned blocks, then optionally read it back."""
    size = os.path.getsize(src)
    if size > dst_size:
        raise RuntimeError(f"The image ({human(size)}) is larger than the drive ({human(dst_size)}).")
    steps = size * (2 if verify else 1) or 1
    h, done = hashlib.sha256(), 0
    with open(src, "rb") as s, open(dst, "r+b", buffering=0) as d:
        while True:
            if cancelled():
                raise Cancelled
            buf = s.read(CHUNK)
            if not buf:
                break
            h.update(buf)
            n = len(buf)
            if n % 4096:
                buf += b"\0" * (4096 - n % 4096)                     # raw drives need whole sectors
            view = memoryview(buf)
            while len(view):
                view = view[d.write(view):]
            done += n
            progress("Writing the image", done, steps)
    if not verify:
        return
    h2, left = hashlib.sha256(), size
    with open(dst, "rb", buffering=0) as d:
        while left > 0:
            if cancelled():
                raise Cancelled
            buf = d.read(min(CHUNK, ((left + 4095) // 4096) * 4096))
            if not buf:
                break
            h2.update(buf[:left])
            step = min(left, len(buf))
            left -= step
            done += step
            progress("Checking what was written", done, steps)
    if left or h2.digest() != h.digest():
        raise RuntimeError("The data read back from the drive does not match the image. The drive may be faulty.")


class UsbDialog(tk.Toplevel):
    """Format a USB drive, or turn it into a bootable installer (like Rufus)."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.configure(bg=PAL["bg"])
        self.title("Bootable USB maker")
        self.resizable(False, False)
        self.q, self.stop, self.worker, self.alive, self.busy = queue.Queue(), threading.Event(), None, True, False
        self.disks, self.iso_ok = [], False
        self.v_disk, self.v_boot = tk.StringVar(), tk.StringVar(value=BOOT_NONE)
        self.v_iso, self.v_scheme = tk.StringVar(), tk.StringVar(value="MBR")
        self.v_fs, self.v_label = tk.StringVar(value="FAT32"), tk.StringVar(value="USB")
        self.v_verify, self.v_big = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        pad = dict(padx=14, pady=5)
        self.columnconfigure(1, weight=1)

        self.warn = ttk.Label(self, wraplength=560, justify="left", padding=(14, 10, 14, 0))
        self.warn.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.adminb = ttk.Button(self, text="Restart as administrator", command=self.elevate)
        ttk.Label(self, text="Drive:").grid(row=2, column=0, sticky="w", **pad)
        self.cb = ttk.Combobox(self, textvariable=self.v_disk, state="readonly", width=64)
        self.cb.grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Button(self, text="Refresh", command=self.refresh).grid(row=2, column=2, padx=14)
        ttk.Checkbutton(self, text="Also list big USB hard drives (over 256 GB)", variable=self.v_big,
                        command=self.refresh).grid(row=3, column=1, sticky="w")
        ttk.Label(self, text="Boot selection:").grid(row=4, column=0, sticky="w", **pad)
        bs = ttk.Combobox(self, textvariable=self.v_boot, state="readonly", values=[BOOT_NONE, BOOT_WIN, BOOT_DD],
                          width=64)
        bs.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(0, 14))
        bs.bind("<<ComboboxSelected>>", lambda _: self.sync())
        ttk.Label(self, text="Image file:").grid(row=5, column=0, sticky="w", **pad)
        self.iso_e = ttk.Entry(self, textvariable=self.v_iso)
        self.iso_e.grid(row=5, column=1, sticky="ew", pady=5)
        self.iso_b = ttk.Button(self, text="Choose...", command=self.pick_iso)
        self.iso_b.grid(row=5, column=2, padx=14)
        self.iso_note = ttk.Label(self, text="", wraplength=560, justify="left")
        self.iso_note.grid(row=6, column=1, columnspan=2, sticky="w", padx=(0, 14))
        ttk.Label(self, text="Partition scheme:").grid(row=7, column=0, sticky="w", **pad)
        self.sch = ttk.Combobox(self, textvariable=self.v_scheme, state="readonly", values=["MBR", "GPT"], width=10)
        self.sch.grid(row=7, column=1, sticky="w")
        ttk.Label(self, text="File system:").grid(row=8, column=0, sticky="w", **pad)
        self.fsb = ttk.Combobox(self, textvariable=self.v_fs, state="readonly", values=["FAT32", "exFAT", "NTFS"],
                                width=10)
        self.fsb.grid(row=8, column=1, sticky="w")
        self.fsb.bind("<<ComboboxSelected>>", lambda _: self.sync())
        ttk.Label(self, text="Volume label:").grid(row=9, column=0, sticky="w", **pad)
        self.lab = ttk.Entry(self, textvariable=self.v_label, width=24)
        self.lab.grid(row=9, column=1, sticky="w")
        self.v_local, self.v_user, self.v_pw = tk.BooleanVar(value=False), tk.StringVar(), tk.StringVar()
        self.v_user.set(os.environ.get("USERNAME", "")[:20] if valid_username(os.environ.get("USERNAME", "")) == "" else "")
        self.winf = ttk.LabelFrame(self, text="Windows setup options", padding=(10, 4))
        self.winf.grid(row=10, column=0, columnspan=3, sticky="ew", padx=14, pady=(8, 2))
        self.winf.columnconfigure(2, weight=1)
        self.chk_local = ttk.Checkbutton(self.winf, text="Create a local account with the user name:",
                                         variable=self.v_local, command=self.sync)
        self.chk_local.grid(row=0, column=0, columnspan=2, sticky="w")
        self.ent_user = ttk.Entry(self.winf, textvariable=self.v_user, width=22)
        self.ent_user.grid(row=0, column=2, sticky="w", padx=6)
        ttk.Label(self.winf, text="Password (optional):").grid(row=1, column=0, sticky="w", padx=(22, 0), pady=3)
        self.ent_pw = ttk.Entry(self.winf, textvariable=self.v_pw, show="*", width=22)
        self.ent_pw.grid(row=1, column=1, columnspan=2, sticky="w", padx=6)
        self.win_note = ttk.Label(self.winf, wraplength=520, justify="left", text=(
            "Skips the Microsoft account / internet screens when Windows is first set up. The new user is an "
            "administrator. Leave the password empty and set one after Windows starts: a password typed here "
            "is stored as readable text on the USB drive."))
        self.win_note.grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.ver = ttk.Checkbutton(self, text="Check the drive after writing (slower, but makes sure it is good)",
                                   variable=self.v_verify)
        self.ver.grid(row=11, column=1, columnspan=2, sticky="w")
        self.hint = ttk.Label(self, wraplength=560, justify="left", padding=(14, 8, 14, 0))
        self.hint.grid(row=12, column=0, columnspan=3, sticky="ew")
        self.prog = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.prog.grid(row=13, column=0, columnspan=3, sticky="ew", padx=14, pady=(10, 0))
        self.msg = ttk.Label(self, text="", wraplength=560, justify="left", padding=(14, 4))
        self.msg.grid(row=14, column=0, columnspan=3, sticky="ew")
        bt = ttk.Frame(self, padding=(14, 4, 14, 14))
        bt.grid(row=15, column=0, columnspan=3, sticky="ew")
        self.startb = ttk.Button(bt, text="Start", style="Accent.TButton", command=self.start)
        self.startb.pack(side="left")
        self.stopb = ttk.Button(bt, text="Cancel", command=self.stop.set, state="disabled")
        self.stopb.pack(side="left", padx=6)
        ttk.Button(bt, text="Close", command=self.close).pack(side="right")
        self.after(100, self.poll)
        self.sync()
        self.refresh()

    # ---- state of the form
    def mode(self):
        return self.v_boot.get()

    def sync(self):
        dd, win = self.mode() == BOOT_DD, self.mode() == BOOT_WIN
        st = lambda w, on: w.config(state="readonly" if on and isinstance(w, ttk.Combobox)
                                    else ("normal" if on else "disabled"))
        st(self.iso_e, dd or win)
        self.iso_b.config(state="normal" if dd or win else "disabled")
        st(self.sch, not dd)
        st(self.fsb, not dd)
        st(self.lab, not dd)
        self.ver.config(state="normal" if dd else "disabled")
        self.chk_local.config(state="normal" if win else "disabled")
        on = win and self.v_local.get()
        self.ent_user.config(state="normal" if on else "disabled")
        self.ent_pw.config(state="normal" if on else "disabled")
        if win and self.v_fs.get() == "exFAT":
            self.v_fs.set("FAT32")
        fs = self.v_fs.get()
        self.v_label.set(clean_label(self.v_label.get(), fs))
        tips = []
        if self.mode() == BOOT_NONE:
            tips.append("Wipes the drive and formats it. Pick exFAT or NTFS for drives over 32 GB: "
                        "FAT32 here is limited to 32 GB.")
        elif win:
            tips.append("Copies a Windows installer ISO onto the drive. FAT32 + MBR starts on nearly every PC. "
                        "A file over 4 GB (install.wim) is split automatically. NTFS only starts on older "
                        "(BIOS) PCs.")
        else:
            tips.append("Writes the image to the drive exactly as it is - for Linux and other 'hybrid' ISOs and "
                        ".img files. The drive then looks unreadable in Windows; that is normal.")
        if fs == "FAT32" and not dd:
            tips.append("FAT32 volumes are capped at 32 GB.")
        self.hint.config(text="  ".join(tips))

    def pick_iso(self):
        f = filedialog.askopenfilename(parent=self, title="Choose the image",
                                       filetypes=[("Disk images", "*.iso *.img *.raw *.bin"), ("All files", "*.*")])
        if f:
            self.v_iso.set(os.path.normpath(f))
            self.check_iso()

    def check_iso(self):
        p = self.v_iso.get().strip().strip('"')
        self.iso_ok = False
        try:
            info = iso_info(p)
        except OSError:
            self.iso_note.config(text="")
            return
        self.iso_ok = True
        text = f"{human(info['size'])}.  "
        if self.mode() == BOOT_DD:
            text += ("Can be written in DD mode." if info["hybrid"] else
                     "This image does not look 'hybrid', so the drive may not start from it. "
                     "Windows installer ISOs need the 'Windows installer' option instead.")
        self.iso_note.config(text=text)

    def refresh(self):
        if self.busy:
            return
        if os.name != "nt":
            self.warn.config(text="Making bootable drives only works on Windows.")
            self.startb.config(state="disabled")
            return
        if is_admin():
            self.warn.config(text="")
            self.adminb.grid_remove()
            self.startb.config(state="normal")
        else:
            self.warn.config(text="MultiZip needs administrator rights to format drives or write to them.")
            self.adminb.grid(row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 0))
            self.startb.config(state="disabled")
        self.msg.config(text="Looking for drives...")
        threading.Thread(target=lambda: self.q.put(("disks",) + list_usb_disks(self.v_big.get())),
                         daemon=True).start()

    def elevate(self):
        try:
            if restart_as_admin():
                self.app.destroy()
                return
        except Exception:
            pass
        messagebox.showinfo(APP, "Windows did not start MultiZip as administrator. "
                                 "Right-click MultiZip and choose 'Run as administrator'.", parent=self)

    def chosen(self):
        i = self.cb.current()
        return self.disks[i] if 0 <= i < len(self.disks) else None

    # ---- run
    def start(self):
        d = self.chosen()
        if self.busy or not d:
            return messagebox.showinfo(APP, "Plug in a USB drive and pick it from the list first.", parent=self)
        mode, iso = self.mode(), self.v_iso.get().strip().strip('"')
        if mode != BOOT_NONE:
            if not iso or not os.path.isfile(iso):
                return messagebox.showinfo(APP, "Choose the ISO / image file first.", parent=self)
            if mode == BOOT_DD and iso_info(iso)["size"] > d["size"]:
                return messagebox.showerror(APP, "That image is larger than the drive.", parent=self)
        if mode == BOOT_WIN and self.v_local.get():
            why = valid_username(self.v_user.get())
            if why:
                return messagebox.showinfo(APP, why, parent=self)
        if d["offline"] or d["readonly"]:
            return messagebox.showerror(APP, "Windows has this drive set to offline or read-only "
                                             "(some drives have a write-protect switch).", parent=self)
        what = {BOOT_NONE: "format it", BOOT_WIN: "format it and copy the Windows installer onto it",
                BOOT_DD: "overwrite it with the image"}[mode]
        answer = simpledialog.askstring(
            APP, f"EVERYTHING on this drive will be erased:\n\n{describe_disk(d)}\n\nMultiZip will {what}.\n"
                 "Check that this is the right drive, then type ERASE to continue.", parent=self)
        if (answer or "").strip().upper() != "ERASE":
            return
        job = dict(disk=d, mode=mode, iso=iso, scheme=self.v_scheme.get(), fs=self.v_fs.get(),
                   label=self.v_label.get(), verify=self.v_verify.get(),
                   local_user=self.v_user.get() if mode == BOOT_WIN and self.v_local.get() else "",
                   local_pw=self.v_pw.get())
        self.stop.clear()
        self.set_busy(True)
        self.prog.config(mode="indeterminate")
        self.prog.start(12)
        self.worker = threading.Thread(target=self.work, args=(job,), daemon=True)
        self.worker.start()

    def set_busy(self, busy):
        self.busy = busy
        self.startb.config(state="disabled" if busy else "normal")
        self.stopb.config(state="normal" if busy else "disabled")

    def say(self, text):
        self.q.put(("say", text))

    def progress(self, what, done, total):
        self.q.put(("prog", what, done, total))

    def work(self, job):
        mount = None
        try:
            d, mode = job["disk"], job["mode"]
            now, err = list_usb_disks(True)                 # look again: the drive must still be exactly this one
            cur = next((x for x in now if x["number"] == d["number"]), None)
            if err or not cur or cur["size"] != d["size"] or cur["name"] != d["name"]:
                raise RuntimeError("The drive list changed while you were choosing. Nothing was changed - "
                                   "press Refresh and try again.")
            if mode == BOOT_DD:
                self.say("Clearing the drive...")
                run_diskpart(script_clean(d["number"]))
                time.sleep(2)
                self.say("Writing the image...")
                write_image(job["iso"], rf"\\.\PhysicalDrive{int(d['number'])}", d["size"], self.progress,
                            self.stop.is_set, job["verify"])
                self.q.put(("done", "Done. The drive is ready to start a computer from. "
                                    "Windows may say it needs formatting - ignore that."))
                return
            fs, win = job["fs"], mode == BOOT_WIN
            size_mb = FAT32_PART_MB if fs == "FAT32" and d["size"] > FAT32_PART_MB * 1024 * 1024 else None
            self.say("Erasing and formatting the drive...")
            run_diskpart(script_format(d["number"], job["scheme"], fs, job["label"], win, size_mb))
            root = wait_for_volume(d["number"], fs, self.stop.is_set)
            if not win:
                self.q.put(("done", f"Done. The drive is formatted as {fs} ({root[:2]})."))
                return
            self.say("Opening the ISO...")
            mount = mount_iso(job["iso"])
            if not is_windows_media(mount):
                raise RuntimeError("That ISO is not a Windows installer. For Linux and other images use "
                                   "'Disk image / Linux ISO (DD mode)'. The drive has been formatted.")
            big = copy_tree(mount, root, lambda a, t, n: self.progress("Copying " + n, a, t), self.stop.is_set,
                            fat32=(fs == "FAT32"))
            for p, rel, _sz in big:
                if os.path.basename(rel).lower() != "install.wim":
                    raise RuntimeError(f"{rel} is over 4 GB and cannot be stored on FAT32. "
                                       "Choose NTFS and try again.")
                self.q.put(("indet", "Splitting install.wim so it fits on FAT32 (this takes a few minutes)..."))
                split_wim(p, os.path.join(root, os.path.dirname(rel), "install.swm"))
            note = ""
            if job["local_user"]:
                self.say("Adding the local account setup file...")
                with open(os.path.join(root, "autounattend.xml"), "w", encoding="utf-8", newline="\r\n") as f:
                    f.write(build_unattend(job["local_user"], job["local_pw"]))
                note += f" Windows will create the local account '{job['local_user']}' during setup."
            if job["scheme"] == "MBR":
                bs = os.path.join(mount, "boot", "bootsect.exe")
                ok = os.path.isfile(bs) and subprocess.run([bs, "/nt60", root[:2], "/force", "/mbr"],
                                                           capture_output=True, timeout=120, **_flags()).returncode == 0
                if not ok:
                    note += " (The old-style BIOS boot code could not be written; newer UEFI PCs will still start.)"
            self.q.put(("done", "Done. The Windows installer drive is ready." + note))
        except Cancelled:
            self.q.put(("fail", "Cancelled. The drive may be half-written - run it again before using it."))
        except Exception as e:
            self.q.put(("fail", str(e) or e.__class__.__name__))
        finally:
            if mount:
                unmount_iso(job["iso"])

    def poll(self):
        if not self.alive:
            return
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "disks":
                    self.disks = m[1]
                    self.cb.config(values=[describe_disk(x) for x in self.disks])
                    if self.disks:
                        self.cb.current(0)
                    else:
                        self.cb.set("")
                    self.msg.config(text=m[2] or ("" if self.disks else "No USB drive found. Plug one in and press Refresh."))
                elif m[0] == "say":
                    self.msg.config(text=m[1])
                elif m[0] == "indet":
                    self.prog.config(mode="indeterminate")
                    self.prog.start(12)
                    self.msg.config(text=m[1])
                elif m[0] == "prog":
                    self.prog.stop()
                    self.prog.config(mode="determinate", value=100 * m[2] / max(1, m[3]))
                    self.msg.config(text=f"{m[1]}   {int(100 * m[2] / max(1, m[3]))}%")
                else:
                    self.prog.stop()
                    self.prog.config(mode="determinate", value=100 if m[0] == "done" else 0)
                    self.set_busy(False)
                    self.msg.config(text=m[1])
                    (messagebox.showinfo if m[0] == "done" else messagebox.showerror)(APP, m[1], parent=self)
                    self.refresh()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def close(self):
        if self.busy and not messagebox.askyesno(APP, "The drive is still being written. Cancel and close?",
                                                 parent=self):
            return
        self.alive = False
        self.stop.set()
        self.destroy()


# ================================================== download manager (engine)
DL_SEG = 1024 * 1024                  # a piece is never split smaller than this
DL_CHUNK = 64 * 1024
DL_TRIES = 6                          # network retries before giving up
DL_TIMEOUT = 15                       # seconds without data before a connection counts as dead
DL_BACKUP = 256 * 1024                # on resume, fetch this much of each piece again to be safe
DL_OPEN = 1 << 62                     # "to the end" when the size is not known
DL_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MultiZip"
DL_HASHES = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}
DL_STATES = ("queued", "active", "paused", "done", "error")
DL_KINDS = {
    "Archives": "zip 7z rar tar gz tgz bz2 tbz2 xz txz zst tzst lzma cab",
    "Disk images": "iso img wim esd swm vhd vhdx vmdk vdi qcow2 dmg",
    "Programs": "exe msi msix appx apk deb rpm jar appimage",
    "Video": "mp4 mkv avi mov wmv flv webm m4v mpg mpeg",
    "Music": "mp3 flac wav aac ogg m4a wma opus",
    "Pictures": "jpg jpeg png gif bmp webp tif tiff svg heic ico",
    "Documents": "pdf doc docx xls xlsx ppt pptx txt rtf odt ods epub csv",
}
DL_KIND_OF = {ext: k for k, exts in DL_KINDS.items() for ext in exts.split()}
DL_CATEGORIES = tuple(DL_KINDS) + ("Other",)
DL_RISKY = {"exe", "msi", "msix", "appx", "bat", "cmd", "ps1", "vbs", "js", "jse", "wsf", "scr", "com", "jar", "lnk",
            "hta", "reg"}


class DlFatal(Exception):
    """A download failed for good (retrying will not help)."""


class DlStop(Exception):
    """The download was paused or removed."""


class DlCtl(threading.Event):
    """Stop switch for one run of one download; `fatal` carries the reason when a piece gave up."""
    fatal = ""


class Seg:
    """One piece of a download: bytes start..end (inclusive); `pos` is the next byte still to fetch."""
    __slots__ = ("start", "end", "pos", "live")

    def __init__(self, start, end, pos=None):
        self.start, self.end, self.live = start, end, False
        self.pos = start if pos is None else pos

    def todo(self):
        return self.end - self.pos + 1


def dl_category(name):
    return DL_KIND_OF.get(os.path.splitext(name)[1].lower().lstrip("."), "Other")


def dl_default_folder():
    return os.path.join(os.path.expanduser("~"), "Downloads")


def dl_state_path():
    return os.path.join(os.path.dirname(settings_path()), "downloads.json")


def dl_find_links(text):
    """Every web link in some pasted text, in order, without repeats."""
    out, seen = [], set()
    for m in re.finditer(r"(?:https?://|www\.)[^\s\"'<>]+", text or "", re.I):
        u = m.group(0).rstrip(".,;")
        if u.lower().startswith("www."):
            u = "https://" + u
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def clean_filename(name, default="download"):
    """Make a server- or URL-supplied name safe: no folders, no characters Windows rejects."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join("_" if (c in BAD_CHARS or ord(c) < 32) else c for c in name).strip(" .")
    if name.split(".")[0].lower() in RESERVED:
        name = "_" + name
    if len(name) > 150:
        root, ext = os.path.splitext(name)
        name = root[:max(1, 150 - len(ext))] + ext
    return name or default


def split_name(name):
    low = name.lower()
    for e in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if low.endswith(e):
            return name[:-len(e)], name[-len(e):]
    return os.path.splitext(name)


def name_from_response(headers, url):
    cd = headers.get("Content-Disposition")
    if cd:
        try:
            msg = email.message.Message()
            msg["content-disposition"] = cd
            fn = msg.get_filename()
            if fn:
                return clean_filename(fn)
        except Exception:
            pass
    return clean_filename(urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]))


def dl_request(url, extra=None):
    h = {"User-Agent": DL_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    h.update(extra or {})
    return urllib.request.Request(url, headers=h)


def dl_probe(url):
    """Ask the server about the file: size, whether it can resume, a name and a version tag."""
    with urllib.request.urlopen(dl_request(url, {"Range": "bytes=0-0"}), timeout=DL_TIMEOUT) as r:
        h = r.headers
        info = dict(size=-1, ranges=False, etag=h.get("ETag") or h.get("Last-Modified") or "",
                    name=name_from_response(h, r.geturl()))
        if r.status == 206:
            info["ranges"] = True
            m = re.match(r"\s*bytes\s+\d+-\d+/(\d+)", h.get("Content-Range", ""))
            if m:
                info["size"] = int(m.group(1))
        else:
            cl = (h.get("Content-Length") or "").strip()
            if cl.isdigit():
                info["size"] = int(cl)
        return info


def dl_explain(e):
    """(is it worth trying again, what to tell the user) for a network error."""
    if isinstance(e, urllib.error.HTTPError):
        return (e.code in (408, 425, 429) or e.code >= 500), f"The server answered {e.code} {e.reason}."
    if isinstance(e, ssl.SSLCertVerificationError) or (
            isinstance(e, urllib.error.URLError) and isinstance(e.reason, ssl.SSLCertVerificationError)):
        return False, "The website's security certificate is not trusted."
    if isinstance(e, urllib.error.URLError):
        return True, f"Could not connect: {e.reason}"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return True, "The server stopped answering."
    if isinstance(e, http.client.IncompleteRead):
        return True, "The connection dropped."
    if isinstance(e, (http.client.HTTPException, OSError)):
        return True, str(e) or type(e).__name__
    return False, str(e) or type(e).__name__


class Throttle:
    """One speed limit shared by every download (bytes per second, 0 = no limit)."""

    def __init__(self):
        self.rate, self.lock, self.free_at = 0, threading.Lock(), 0.0

    def take(self, n, stop):
        if not self.rate:
            return
        with self.lock:
            now = time.monotonic()
            self.free_at = max(self.free_at, now) + n / self.rate
            wait = self.free_at - now - 0.2          # a little burst keeps the average honest
        if wait > 0:
            stop.wait(wait)


class DlItem:
    def __init__(self, url, folder, conns=8, check=""):
        self.id = uuid.uuid4().hex[:10]
        self.url, self.folder, self.check = url, folder, check.strip().lower()
        self.conns = max(1, min(16, int(conns)))
        self.name, self.named = "", False        # `named`: the server has told us the real name
        self.size, self.got = -1, 0
        self.state, self.error, self.bad = "queued", "", False
        self.added, self.finished = time.time(), 0.0
        self.ranges, self.etag, self.segs = False, "", []
        self.busy, self.ctl, self.speed, self.stage = False, None, 0.0, ""      # runtime only

    def part(self):
        return os.path.join(self.folder, self.name + ".part")

    def target(self):
        return os.path.join(self.folder, self.name)

    def to_json(self):
        d = {k: getattr(self, k) for k in ("id", "url", "folder", "check", "conns", "name", "named", "size", "got",
                                           "state", "error", "bad", "added", "finished", "ranges", "etag")}
        d["segs"] = [[s.start, s.end, s.pos] for s in self.segs]
        return d

    @classmethod
    def from_json(cls, d):
        it = cls(str(d["url"]), str(d["folder"]), d.get("conns", 8), str(d.get("check", "")))
        it.id, it.name, it.error, it.etag = (str(d.get(k, "")) or getattr(it, k) for k in ("id", "name", "error", "etag"))
        it.named, it.bad, it.ranges = bool(d.get("named")), bool(d.get("bad")), bool(d.get("ranges"))
        it.size, it.got = int(d.get("size", -1)), int(d.get("got", 0))
        it.added, it.finished = float(d.get("added", 0)), float(d.get("finished", 0))
        it.state = d.get("state") if d.get("state") in DL_STATES else "paused"
        if it.state == "active":
            it.state = "paused"
        for a, b, c in d.get("segs", []):
            if 0 <= a <= c <= b + 1:
                it.segs.append(Seg(int(a), int(b), int(c)))
        return it


class Downloader:
    """Keeps the list of downloads, runs them (several pieces at once), and remembers them between sessions."""

    def __init__(self, path=None):
        self.lock, self.io = threading.RLock(), threading.Lock()
        self.items, self.events = [], queue.Queue()          # newest first
        self.throttle, self.max_active, self.closing = Throttle(), 3, False
        self.path, self.last_save = path or dl_state_path(), 0.0
        self.load()

    # ---- remembering
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for d in data.get("items", []) if isinstance(data, dict) else []:
            try:
                self.items.append(DlItem.from_json(d))
            except Exception:
                continue
        self.items = self.items[:500]

    def save(self, force=False):
        with self.lock:
            now = time.monotonic()
            if not force and now - self.last_save < 1.0:
                return
            self.last_save = now
            data = {"v": 1, "items": [i.to_json() for i in self.items]}
        with self.io:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self.path)
            except OSError:
                pass

    # ---- the list
    def unique(self, folder, name, me=None):
        """`name`, or `name (2)` etc., so it clashes with no file and no other download in that folder."""
        taken = {i.name.lower() for i in self.items if i is not me and i.folder == folder and i.state != "done"}
        base, ext = split_name(name)
        n, cand = 1, name
        while (cand.lower() in taken or os.path.lexists(os.path.join(folder, cand))
               or os.path.lexists(os.path.join(folder, cand + ".part"))):
            n += 1
            cand = f"{base} ({n}){ext}"
        return cand

    def add(self, urls, folder, conns=8, check=""):
        """Queue links. Returns (how many were added, a list of problems)."""
        made, problems = 0, []
        check = check.strip().lower()
        if check and (len(urls) != 1 or len(check) not in DL_HASHES or not re.fullmatch(r"[0-9a-f]+", check)):
            return 0, ["The checksum must be MD5, SHA-1, SHA-256 or SHA-512, and works for one link at a time."]
        folder = os.path.abspath(os.path.expanduser(folder.strip().strip('"')))
        with self.lock:
            for u in urls:
                u = u.strip()
                parts = urllib.parse.urlsplit(u)
                if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
                    problems.append(f"{u[:70]}: only http:// and https:// links work.")
                    continue
                it = DlItem(u, folder, conns, check)
                it.name = self.unique(folder, clean_filename(urllib.parse.unquote(parts.path.rsplit("/", 1)[-1])), it)
                self.items.insert(0, it)
                made += 1
        self.save(True)
        self.schedule()
        return made, problems

    def find(self, ids):
        ids = set(ids)
        return [i for i in self.items if i.id in ids]

    def view(self, it):
        return dict(id=it.id, name=it.name, url=it.url, folder=it.folder, path=it.target(), size=it.size, got=it.got,
                    state=it.state, error=it.error, bad=it.bad, speed=it.speed if it.state == "active" else 0.0,
                    stage=it.stage, live=sum(1 for s in it.segs if s.live), added=it.added, finished=it.finished,
                    cat=dl_category(it.name), check=it.check)

    def snapshot(self):
        with self.lock:
            return [self.view(i) for i in self.items]

    def summary(self):
        """(downloading, waiting, bytes per second)."""
        with self.lock:
            act = [i for i in self.items if i.state == "active"]
            return len(act), sum(1 for i in self.items if i.state == "queued"), sum(i.speed for i in act)

    def running(self):
        a, q, _ = self.summary()
        return a + q > 0

    # ---- controls
    def schedule(self):
        """Start waiting downloads while there is room (oldest first)."""
        with self.lock:
            if self.closing:
                return
            running = sum(1 for i in self.items if i.state == "active")
            for it in reversed(self.items):
                if running >= self.max_active:
                    break
                if it.state == "queued" and not it.busy:
                    it.state, it.busy, it.stage, it.ctl = "active", True, "Connecting...", DlCtl()
                    threading.Thread(target=self._run, args=(it, it.ctl), daemon=True).start()
                    running += 1

    def pause(self, ids):
        with self.lock:
            for it in self.find(ids):
                if it.state in ("active", "queued"):
                    it.state, it.speed = "paused", 0.0
                    if it.ctl:
                        it.ctl.set()
        self.save(True)
        self.schedule()

    def resume(self, ids):
        with self.lock:
            for it in self.find(ids):
                if it.state in ("paused", "error"):
                    if it.bad:
                        self._reset(it)
                    it.state, it.error = "queued", ""
        self.save(True)
        self.schedule()

    def restart(self, ids):
        """Throw the data away and download again from the start."""
        with self.lock:
            for it in self.find(ids):
                if it.ctl:
                    it.ctl.set()
                self._reset(it)
                it.state, it.error, it.finished = "queued", "", 0.0
        self.save(True)
        self.schedule()

    def _forget(self, it):
        it.segs, it.got, it.speed = [], 0, 0.0

    def _reset(self, it):
        """Start over: forget the progress and delete the half-made file (and a finished one that is damaged)."""
        gone = [it.part()] + ([it.target()] if it.bad else [])
        it.bad = False
        self._forget(it)
        for p in gone:
            try:
                os.remove(p)
            except OSError:
                pass

    def remove(self, ids, delete_files=False):
        with self.lock:
            gone = self.find(ids)
            for it in gone:
                if it.ctl:
                    it.ctl.set()
                it.state = "paused" if it.state != "done" else it.state
            self.items = [i for i in self.items if i not in gone]
        for it in gone:
            files = [it.part()] + ([it.target()] if (delete_files and (it.state == "done" or it.bad)) else [])
            threading.Thread(target=self._unlink, args=(it, files, True), daemon=True).start()
        self.save(True)
        self.schedule()

    def _unlink(self, it, files, to_bin):
        for _ in range(100):                               # wait for its pieces to let go of the file
            if not it.busy:
                break
            time.sleep(0.2)
        for p in files:
            try:
                if os.path.lexists(p):
                    if to_bin and not p.endswith(".part") and recycle([p]):
                        continue
                    os.remove(p)
            except OSError:
                pass

    def shutdown(self):
        """Pause everything and save, so the next start can carry on."""
        with self.lock:
            self.closing = True
            for it in self.items:
                if it.state == "active":
                    it.state = "paused"
                if it.ctl:
                    it.ctl.set()
        for _ in range(60):
            if not any(i.busy for i in self.items):
                break
            time.sleep(0.1)
        self.save(True)

    # ---- one download
    def _run(self, it, ctl):
        try:
            self._prepare(it, ctl)
            self._transfer(it, ctl)
            if ctl.fatal:
                raise DlFatal(ctl.fatal)
            if not ctl.is_set():
                self._finish(it, ctl)
        except DlStop:
            pass
        except DlFatal as e:
            self._fail(it, ctl, str(e))
        except Exception as e:
            self._fail(it, ctl, dl_explain(e)[1])
        finally:
            with self.lock:
                it.busy, it.speed, it.stage = False, 0.0, ""
            self.save(True)
            self.schedule()

    def _fail(self, it, ctl, text):
        ctl.set()
        with self.lock:
            if it.state == "active":
                it.state, it.error = "error", text
                self.events.put(("error", it.id))

    def _retrying(self, ctl, fn):
        for attempt in range(DL_TRIES + 1):
            try:
                return fn()
            except Exception as e:
                if ctl.is_set():
                    raise DlStop
                retry, text = dl_explain(e)
                if not retry or attempt >= DL_TRIES:
                    raise DlFatal(text)
                ctl.wait(min(2 ** attempt, 20))

    def _prepare(self, it, ctl):
        info = self._retrying(ctl, lambda: dl_probe(it.url))
        if ctl.is_set():
            raise DlStop
        try:
            os.makedirs(it.folder, exist_ok=True)
        except OSError as e:
            raise DlFatal(f"Could not create the folder: {e}")
        try:
            have = os.path.getsize(it.part()) if os.path.isfile(it.part()) else -1
        except OSError:
            have = -1
        with self.lock:
            same = (bool(it.segs) and it.size > 0 and info["size"] == it.size and info["ranges"] and have == it.size
                    and not (it.etag and info["etag"] and it.etag != info["etag"]))
            if same:                                       # carry on where it stopped
                for s in it.segs:
                    if s.pos <= s.end:
                        s.pos = max(s.start, s.pos - DL_BACKUP)
                it.got, it.stage = sum(s.pos - s.start for s in it.segs), ""
                return
            self._forget(it)                               # new, or the file changed, or it cannot resume
            if not it.named:
                it.name, it.named = self.unique(it.folder, info["name"] or it.name, it), True
            it.size, it.ranges, it.etag = info["size"], info["ranges"], info["etag"]
            if it.size > 0:
                n = 1
                if it.ranges and it.size >= 2 * DL_SEG:
                    n = max(1, min(it.conns, it.size // DL_SEG))
                cut = [it.size * k // n for k in range(n + 1)]
                it.segs = [Seg(cut[k], cut[k + 1] - 1) for k in range(n)]
            elif it.size < 0:
                it.segs = [Seg(0, DL_OPEN)]
            size, part = it.size, it.part()
        try:
            free = shutil.disk_usage(it.folder).free
        except OSError:
            free = None
        if size > 0 and free is not None and free < size + DL_SEG:
            raise DlFatal(f"Not enough free space: {human(size)} needed, {human(free)} free.")
        try:
            with open(part, "wb") as f:
                if size > 0:
                    f.truncate(size)
        except OSError as e:
            raise DlFatal(f"Could not create the file: {e}")
        with self.lock:
            it.stage = ""

    def _transfer(self, it, ctl):
        threads, part = [], it.part()
        t0, g0 = time.monotonic(), it.got
        while not ctl.is_set():
            with self.lock:
                todo = [s for s in it.segs if s.pos <= s.end]
                if not todo:
                    break
                live = sum(1 for s in todo if s.live)
                for s in todo:
                    if not s.live and live < it.conns:
                        s.live, live = True, live + 1
                        t = threading.Thread(target=self._fetch, args=(it, s, ctl, part), daemon=True)
                        threads.append(t)
                        t.start()
                if live < it.conns and it.ranges and all(s.live for s in todo):
                    big = max(todo, key=Seg.todo)           # a connection is free: take half of the biggest piece
                    if big.todo() >= 2 * DL_SEG:
                        mid = big.pos + big.todo() // 2
                        it.segs.append(Seg(mid, big.end))
                        big.end = mid - 1
            now = time.monotonic()
            if now - t0 >= 0.5:
                with self.lock:
                    inst = (it.got - g0) / (now - t0)
                    it.speed = inst if it.speed == 0 else 0.6 * it.speed + 0.4 * inst
                    t0, g0 = now, it.got
            self.save()
            ctl.wait(0.2)
        for t in threads:
            t.join()

    def _fetch(self, it, seg, ctl, part):
        """Download one piece, reconnecting from where it stopped if the connection breaks."""
        tries = 0
        try:
            with open(part, "r+b") as f:
                while not ctl.is_set():
                    with self.lock:
                        if seg.pos > seg.end:
                            return
                        pos, end = seg.pos, seg.end
                    head = {"Range": f"bytes={pos}-" + ("" if end >= DL_OPEN else str(end))} if it.ranges else {}
                    try:
                        with urllib.request.urlopen(dl_request(it.url, head), timeout=DL_TIMEOUT) as r:
                            if it.ranges and r.status != 206 and pos > 0:
                                raise DlFatal("The server no longer supports resuming this download.")
                            if r.status == 206:
                                m = re.match(r"\s*bytes\s+(\d+)-", r.headers.get("Content-Range", ""))
                                if not m or int(m.group(1)) != pos:
                                    raise DlFatal("The server sent the wrong part of the file.")
                            while not ctl.is_set():
                                with self.lock:
                                    room = seg.end - seg.pos + 1
                                if room <= 0:
                                    return
                                chunk = r.read(min(DL_CHUNK, room))
                                if not chunk:
                                    if it.size >= 0:
                                        raise http.client.IncompleteRead(b"")
                                    with self.lock:         # size unknown: the end of the stream is the end of the file
                                        seg.end = seg.pos - 1
                                    return
                                self.throttle.take(len(chunk), ctl)
                                with self.lock:
                                    at = seg.pos
                                    n = max(0, min(len(chunk), seg.end - at + 1))
                                    seg.pos, it.got = at + n, it.got + n
                                if n:
                                    try:
                                        f.seek(at)
                                        f.write(chunk[:n] if n < len(chunk) else chunk)
                                    except OSError as e:
                                        raise DlFatal(f"Could not write to the disk: {e}")
                                tries = 0
                                if n < len(chunk):
                                    return
                    except DlFatal:
                        raise
                    except Exception as e:
                        if ctl.is_set():
                            return
                        retry, text = dl_explain(e)
                        tries += 1
                        if not retry or tries > DL_TRIES:
                            raise DlFatal(text)
                        if not it.ranges:                  # cannot continue half way: start the file again
                            with self.lock:
                                it.got -= seg.pos - seg.start
                                seg.pos = seg.start
                        ctl.wait(min(2 ** (tries - 1), 20))
        except DlFatal as e:
            ctl.fatal = ctl.fatal or str(e)
            ctl.set()
        except Exception as e:
            ctl.fatal = ctl.fatal or dl_explain(e)[1]
            ctl.set()
        finally:
            with self.lock:
                seg.live = False

    def _finish(self, it, ctl):
        part = it.part()
        with self.lock:
            it.stage = "Finishing..."
        try:
            if it.size < 0:
                it.size = it.got
                with open(part, "r+b") as f:
                    f.truncate(it.got)
            if os.path.getsize(part) != it.size:
                raise DlFatal("The downloaded file is the wrong size. Try again.")
        except OSError as e:
            raise DlFatal(str(e))
        mismatch = False
        if it.check:
            with self.lock:
                it.stage = "Checking the checksum..."
            h = hashlib.new(DL_HASHES[len(it.check)])
            with open(part, "rb") as f:
                while True:
                    if ctl.is_set():
                        raise DlStop
                    b = f.read(1024 * 1024)
                    if not b:
                        break
                    h.update(b)
            mismatch = h.hexdigest() != it.check
        with self.lock:
            name = self.unique(it.folder, it.name, it) if os.path.lexists(it.target()) else it.name
        try:
            os.replace(part, os.path.join(it.folder, name))
        except OSError as e:
            raise DlFatal(f"Could not save the file: {e}")
        mark_web(os.path.join(it.folder, name), it.url)                # Windows will warn before a program runs
        with self.lock:
            it.name, it.segs, it.got, it.finished = name, [], it.size, time.time()
            if mismatch:
                it.state, it.bad = "error", True
                it.error = "The checksum does not match: the file is damaged or was changed."
                self.events.put(("error", it.id))
            else:
                it.state, it.error = "done", ""
                self.events.put(("done", it.id))


# ================================================== download manager (window)
DL_LIMITS = (("Unlimited", 0), ("128 KB/s", 128), ("256 KB/s", 256), ("512 KB/s", 512), ("1 MB/s", 1024),
             ("2 MB/s", 2048), ("5 MB/s", 5120), ("10 MB/s", 10240), ("25 MB/s", 25600))


def dl_eta(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}h {sec % 3600 // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec}s"


def dl_group(v):
    return {"active": "f:run", "queued": "f:run", "done": "f:done"}.get(v["state"], "f:stop")


def dl_row(v):
    """The list columns for one download: size, progress, speed, time left, status."""
    size, got, st = v["size"], v["got"], v["state"]
    if st == "done":
        prog = "Finished"
    elif size > 0:
        prog = share_bar(100.0 * got / size)
    else:
        prog = human(got) if got else ""
    fast = v["speed"] >= 1
    if st == "active":
        status = v["stage"] or (f"Downloading  ({v['live']} connection{'s' if v['live'] != 1 else ''})"
                                if v["live"] else "Starting...")
    elif st == "done":
        status = "Finished  " + datetime.fromtimestamp(v["finished"]).strftime("%Y-%m-%d %H:%M")
    else:
        status = {"queued": "Waiting in the queue", "paused": "Paused"}.get(st, v["error"])
    return (human(size) if size >= 0 else "?", prog, f"{human(v['speed'])}/s" if fast else "",
            dl_eta((size - got) / v["speed"]) if st == "active" and size > 0 and fast else "", status)


class AddDownloadDialog(Dialog):
    """Ask for the links to download, where to save them, and how many connections to use."""

    def __init__(self, parent, S, links=""):
        super().__init__(parent, "Add download")
        ttk.Label(self, text="Links  (one per line, or paste a whole piece of text):").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 4))
        self.txt = tk.Text(self, width=72, height=7, wrap="none", bg=PAL["lbg"], fg=PAL["lfg"],
                           insertbackground=PAL["lfg"], relief="flat", highlightthickness=1,
                           highlightbackground=PAL["border"], font=fnt(10))
        self.txt.grid(row=1, column=0, columnspan=3, sticky="ew", padx=12)
        if links:
            self.txt.insert("1.0", links)
        ttk.Label(self, text="Save to:").grid(row=2, column=0, sticky="w", padx=12, pady=(10, 0))
        self.folder = tk.StringVar(value=S.get("dl_folder") or dl_default_folder())
        ttk.Entry(self, textvariable=self.folder).grid(row=2, column=1, sticky="ew", pady=(10, 0))
        ttk.Button(self, text="Browse...", command=self.browse).grid(row=2, column=2, padx=12, pady=(10, 0))
        ttk.Label(self, text="Connections:").grid(row=3, column=0, sticky="w", padx=12, pady=(8, 0))
        self.conns = tk.StringVar(value=str(S["dl_conns"]))
        ttk.Combobox(self, textvariable=self.conns, values=["1", "2", "4", "8", "16"], width=6,
                     state="readonly").grid(row=3, column=1, sticky="w", pady=(8, 0))
        ttk.Label(self, text="Checksum:").grid(row=4, column=0, sticky="w", padx=12, pady=(8, 0))
        self.check = tk.StringVar()
        ttk.Entry(self, textvariable=self.check).grid(row=4, column=1, columnspan=2, sticky="ew", padx=(0, 12),
                                                      pady=(8, 0))
        ttk.Label(self, wraplength=560, justify="left",
                  text="More connections can make big files download faster. Optional: paste the SHA-256 "
                       "(or MD5, SHA-1, SHA-512) from the download page and MultiZip checks the file when it "
                       "finishes. A checksum works for one link at a time.").grid(
            row=5, column=0, columnspan=3, sticky="w", padx=12, pady=(8, 0))
        self.finish(6, "Download", self.ok)

    def browse(self):
        d = filedialog.askdirectory(parent=self, title="Save downloads in", initialdir=self.folder.get() or None)
        if d:
            self.folder.set(os.path.normpath(d))

    def ok(self):
        links = dl_find_links(self.txt.get("1.0", "end"))
        check, folder = self.check.get().strip(), self.folder.get().strip().strip('"')
        if not links:
            return messagebox.showinfo(APP, "Paste at least one link that starts with http:// or https://",
                                       parent=self)
        if not folder:
            return messagebox.showinfo(APP, "Choose a folder to save the downloads in.", parent=self)
        if check and (len(links) != 1 or len(check) not in DL_HASHES or not re.fullmatch(r"[0-9a-fA-F]+", check)):
            return messagebox.showinfo(APP, "A checksum must be MD5, SHA-1, SHA-256 or SHA-512 (letters and "
                                            "digits only), and it works for one link at a time.", parent=self)
        try:
            conns = int(self.conns.get())
        except ValueError:
            conns = 8
        self.result = dict(urls=links, folder=folder, conns=conns, check=check)
        self.destroy()


class DownloadsDialog(ToolWindow):
    """A download manager: a queue with pause and resume, several connections per file, a speed limit."""
    FILTERS = (("f:all", "All downloads"), ("f:run", "Downloading"), ("f:done", "Finished"),
               ("f:stop", "Paused or failed"))

    def __init__(self, app):
        super().__init__(app)
        self.app, self.dl = app, app.dl
        self.configure(bg=PAL["bg"])
        self.title("Download manager")
        self.geometry("1040x600")
        self.minsize(860, 440)
        self.stop, self.alive, self.filt, self.shown = threading.Event(), True, "f:all", {}
        self.search = tk.StringVar()
        self.protocol("WM_DELETE_WINDOW", self.close)
        S = app.S

        bar = ttk.Frame(self, padding=(12, 12, 12, 6))
        bar.pack(fill="x")
        for text, cmd, tip, style in (
                ("＋  Add download", self.add, "Paste one or more links to download", "Accent.TButton"),
                ("Resume", self.resume, "Start or continue the selected downloads", "TButton"),
                ("Pause", self.pause, "Pause the selected downloads. You can resume them later.", "TButton"),
                ("Resume all", lambda: self.dl.resume([v["id"] for v in self.dl.snapshot()]),
                 "Start every paused or failed download", "TButton"),
                ("Pause all", lambda: self.dl.pause([v["id"] for v in self.dl.snapshot()]),
                 "Pause everything that is downloading or waiting", "TButton"),
                ("Remove", self.remove, "Take the selected downloads off the list (files are kept)", "TButton")):
            b = ttk.Button(bar, text=text, command=cmd, style=style)
            b.pack(side="left", padx=(0, 6))
            Tip(b, tip)
        ttk.Entry(bar, textvariable=self.search, width=24).pack(side="right")
        ttk.Label(bar, text="Search:").pack(side="right", padx=(0, 6))
        self.search.trace_add("write", lambda *_: self.sync())

        body = ttk.Frame(self, padding=(12, 0))
        body.pack(fill="both", expand=True)
        side = ttk.Frame(body, width=190)
        side.pack(side="left", fill="y", padx=(0, 8))
        side.pack_propagate(False)
        self.side = ttk.Treeview(side, show="tree", selectmode="browse")
        self.side.pack(fill="both", expand=True)
        for iid, text in self.FILTERS:
            self.side.insert("", "end", iid=iid, text=text)
        for c in DL_CATEGORIES:
            self.side.insert("", "end", iid="c:" + c, text=c)
        self.side.selection_set("f:all")
        self.side.bind("<<TreeviewSelect>>", self.on_side)
        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True)
        self.tree = make_tree(main, "Name", [("size", "Size", 90, "e"), ("prog", "Progress", 160, "w"),
                                             ("speed", "Speed", 90, "e"), ("eta", "Time left", 80, "e"),
                                             ("status", "Status", 250, "w")])
        self.tree.bind("<Double-1>", self.on_double)
        bind_right_click(self.tree, self.on_menu)
        self.tree.bind("<Delete>", lambda _: self.remove())
        self.menu = tk.Menu(self, tearoff=0, bg=PAL["lbg"], fg=PAL["lfg"], activebackground=PAL["accent"],
                            activeforeground=PAL["accent_fg"], bd=0, font=fnt(10))
        for entry in (("Open", self.open_file), ("Show in folder", self.show_folder),
                      ("Open in MultiZip", self.open_in_app), ("Extract here", lambda: self.extract(False)),
                      ("Extract to a new folder", lambda: self.extract(True)), None,
                      ("Resume", self.resume), ("Pause", self.pause), ("Download again", self.again),
                      ("Copy link", self.copy_link), None,
                      ("Remove from the list", self.remove),
                      ("Remove and delete the file", lambda: self.remove(True))):
            if entry is None:
                self.menu.add_separator()
            else:
                self.menu.add_command(label=entry[0], command=entry[1])

        bt = ttk.Frame(self, padding=(12, 8, 12, 12))
        bt.pack(fill="x")
        ttk.Label(bt, text="Speed limit:").pack(side="left")
        kb = S["dl_limit"]
        labels = [t for t, _ in DL_LIMITS] + ([] if kb in dict((v, t) for t, v in DL_LIMITS) else [f"{kb} KB/s"])
        self.limit = tk.StringVar(value={v: t for t, v in DL_LIMITS}.get(kb, f"{kb} KB/s"))
        cb = ttk.Combobox(bt, textvariable=self.limit, values=labels, state="readonly", width=11)
        cb.pack(side="left", padx=(6, 16))
        cb.bind("<<ComboboxSelected>>", self.on_limit)
        Tip(cb, "The most the downloads may use together.")
        ttk.Label(bt, text="At the same time:").pack(side="left")
        self.act = tk.StringVar(value=str(S["dl_active"]))
        sp = ttk.Spinbox(bt, from_=1, to=10, width=4, textvariable=self.act, command=self.on_active)
        sp.pack(side="left", padx=(6, 16))
        sp.bind("<Return>", lambda _: self.on_active())
        sp.bind("<FocusOut>", lambda _: self.on_active())
        Tip(sp, "How many downloads run together. The others wait in the queue.")
        self.ext = tk.BooleanVar(value=S["dl_extract"])
        ttk.Checkbutton(bt, text="Extract archives when they finish", variable=self.ext,
                        command=self.on_ext).pack(side="left")
        ttk.Button(bt, text="Close", command=self.close).pack(side="right")
        self.total = ttk.Label(bt, text="")
        self.total.pack(side="right", padx=12)
        self.after(100, self.poll)

    # ---- keeping the list up to date
    def poll(self):
        if not self.alive:
            return
        self.sync()
        self.after(500, self.poll)

    def matches(self, v):
        ok = dl_group(v) == self.filt or self.filt == "f:all" if self.filt[0] == "f" else v["cat"] == self.filt[2:]
        q = self.search.get().strip().lower()
        return ok and (not q or q in v["name"].lower() or q in v["url"].lower())

    def sync(self):
        snap = self.dl.snapshot()
        counts, cats = dict.fromkeys((i for i, _ in self.FILTERS), 0), dict.fromkeys(DL_CATEGORIES, 0)
        for v in snap:
            counts["f:all"] += 1
            counts[dl_group(v)] += 1
            cats[v["cat"]] += 1
        for iid, text in self.FILTERS:
            self.side.item(iid, text=f"{text}  ({counts[iid]})")
        for c in DL_CATEGORIES:
            self.side.item("c:" + c, text=f"{c}  ({cats[c]})")
        vis = [v for v in snap if self.matches(v)]
        want = {v["id"] for v in vis}
        for iid in [i for i in self.shown if i not in want]:
            self.tree.delete(iid)
            del self.shown[iid]
        for i, v in enumerate(vis):
            iid, row = v["id"], dl_row(v)
            if iid not in self.shown:
                self.tree.insert("", i, iid=iid, text=v["name"], image=self.icon(v), values=row)
                self.shown[iid] = (v["name"], row)
            elif self.shown[iid] != (v["name"], row):
                self.tree.item(iid, text=v["name"], image=self.icon(v), values=row)
                self.shown[iid] = (v["name"], row)
            if self.tree.index(iid) != i:
                self.tree.move(iid, "", i)
        n, q, speed = self.dl.summary()
        self.total.config(text=(f"{n} downloading" + (f", {q} waiting" if q else "") + f"  -  {human(speed)}/s")
                          if n or q else "")

    @staticmethod
    def icon(v):
        try:
            return icon_for(v["path"]) or ""
        except Exception:
            return ""

    def on_side(self, _=None):
        sel = self.side.selection()
        if sel:
            self.filt = sel[0]
            self.sync()

    # ---- helpers
    def chosen(self):
        by = {v["id"]: v for v in self.dl.snapshot()}
        return [by[i] for i in self.tree.selection() if i in by]

    def ids(self):
        return [v["id"] for v in self.chosen()]

    def one_file(self):
        """The finished file of the first selected download, or None (with a message)."""
        for v in self.chosen():
            if v["state"] == "done" or v["bad"]:
                if os.path.isfile(v["path"]):
                    return v
                messagebox.showinfo(APP, "That file is not there any more. It may have been moved or deleted.",
                                    parent=self)
                return None
        messagebox.showinfo(APP, "Select a finished download first.", parent=self)
        return None

    # ---- actions
    def add(self):
        try:
            clip = "\n".join(dl_find_links(self.clipboard_get())[:20])
        except tk.TclError:
            clip = ""
        d = AddDownloadDialog(self, self.app.S, clip)
        self.wait_window(d)
        r = d.result
        if not r:
            return
        self.app.S["dl_folder"], self.app.S["dl_conns"] = r["folder"], r["conns"]
        save_settings(self.app.S)
        _, problems = self.dl.add(r["urls"], r["folder"], r["conns"], r["check"])
        if problems:
            messagebox.showwarning(APP, "\n".join(problems[:8]), parent=self)
        self.sync()

    def resume(self):
        self.dl.resume(self.ids())
        self.sync()

    def pause(self):
        self.dl.pause(self.ids())
        self.sync()

    def again(self):
        if self.ids() and messagebox.askyesno(APP, "Download the selected files again from the start?", parent=self):
            self.dl.restart(self.ids())
            self.sync()

    def remove(self, delete_files=False):
        sel = self.chosen()
        if not sel:
            return
        if delete_files and not messagebox.askyesno(
                APP, f"Remove {len(sel)} download(s) and delete the file(s) from your computer?\n\n"
                     "Finished files go to the Recycle Bin when it is available.", parent=self):
            return
        self.dl.remove([v["id"] for v in sel], delete_files)
        self.sync()

    def copy_link(self):
        sel = self.chosen()
        if sel:
            self.clipboard_clear()
            self.clipboard_append("\n".join(v["url"] for v in sel))

    def open_file(self):
        v = self.one_file()
        if not v:
            return
        ext = os.path.splitext(v["name"])[1].lower().lstrip(".")
        if ext in DL_RISKY and not messagebox.askyesno(
                APP, f"{v['name']} is a program or script.\nOnly run it if you trust where it came from.\n\n"
                     "Run it now?", parent=self):
            return
        try:
            open_path(v["path"])
        except Exception as e:
            messagebox.showerror(APP, str(e), parent=self)

    def show_folder(self):
        sel = self.chosen()
        if sel:
            v = sel[0]
            if os.path.isfile(v["path"]):
                reveal_file(v["path"])
            else:
                open_folder(v["folder"])

    def archive_file(self):
        v = self.one_file()
        if v and not kind(v["path"]):
            messagebox.showinfo(APP, "MultiZip cannot open that file as an archive.", parent=self)
            return None
        return v

    def open_in_app(self):
        v = self.archive_file()
        if v:
            self.app.show("files")
            self.app.goto(v["path"])
            self.app.deiconify()
            self.app.lift()

    def extract(self, sub):
        v = self.archive_file()
        if v:
            self.app.dl_extract(v["path"], sub)

    def on_double(self, e):
        iid = self.tree.identify_row(e.y)
        v = next((x for x in self.dl.snapshot() if x["id"] == iid), None)
        if not v:
            return
        if v["state"] == "done":
            self.tree.selection_set(iid)
            self.open_file()
        elif v["state"] == "error":
            messagebox.showinfo(APP, f"{v['name']}\n\n{v['error']}", parent=self)
        elif v["state"] == "paused":
            self.dl.resume([iid])
        else:
            self.dl.pause([iid])

    def on_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        if self.tree.selection():
            try:
                self.menu.tk_popup(e.x_root, e.y_root)
            finally:
                self.menu.grab_release()

    # ---- settings kept at the bottom
    def on_limit(self, _=None):
        kb = {t: v for t, v in DL_LIMITS}.get(self.limit.get())
        if kb is None:
            return
        self.app.S["dl_limit"] = kb
        self.dl.throttle.rate = kb * 1024
        save_settings(self.app.S)

    def on_active(self):
        try:
            n = max(1, min(10, int(self.act.get())))
        except ValueError:
            n = self.app.S["dl_active"]
        self.act.set(str(n))
        self.app.S["dl_active"] = self.dl.max_active = n
        save_settings(self.app.S)
        self.dl.schedule()

    def on_ext(self):
        self.app.S["dl_extract"] = bool(self.ext.get())
        save_settings(self.app.S)


# ================================================== archive creation flow
def build_archive(root, paths, out, fmt, lvl, pwd, reveal, S):
    try:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    except OSError as e:
        messagebox.showerror(APP, str(e), parent=root)
        return None
    total = len(paths) if (fmt == "7z" or uses_engine_create(fmt, pwd)) else len(gather(paths))
    tmp = temp_beside(out)
    err = run_job(root, "Creating archive", total,
                  lambda t, c: create_archive(tmp, paths, fmt, lvl, pwd, t, c))
    if not err:
        try:
            os.replace(tmp, out)                         # only now does it replace an archive of the same name
        except OSError as e:
            err = f"Could not save {os.path.basename(out)}: {e}"
    if err:
        try:
            if os.path.exists(tmp):                      # a failed or cancelled run never touches an existing archive
                os.remove(tmp)
        except OSError:
            pass
        if err != "Cancelled":
            messagebox.showerror(APP, err, parent=root)
        return None
    S.update(fmt=fmt, level=lvl, reveal=reveal)
    save_settings(S)
    if reveal:
        reveal_file(out)
    return out


def add_to_archive(root, paths, S):
    dlg = AddDialog(root, paths, S)
    root.wait_window(dlg)
    r = dlg.result
    if not r:
        return None
    return build_archive(root, r["paths"], r["out"], r["fmt"], r["lvl"], r["pwd"], r["reveal"], S)


def quick_archive(root, paths, S):
    fmt = S["fmt"] if S["fmt"] in formats() else "zip"
    out = suggest_name(paths, fmt)
    base, n = out, 1
    while os.path.exists(out):
        stem_, ext = base[: -len(fmt) - 1], "." + fmt
        out = f"{stem_} ({n}){ext}"
        n += 1
    return build_archive(root, [os.path.abspath(p) for p in paths], out, fmt, S["level"], None, S["reveal"], S)


# ======================================================= desktop (Home) view
IMG_EXT = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}
TYPES = (
    ("archive", "#7c3aed", {e.lstrip(".") for e in ALL} | {"tbz2", "txz", "tgz"}),
    ("image", "#16a34a", IMG_EXT | {"svg", "ico", "tif", "tiff"}),
    ("pdf", "#dc2626", {"pdf"}),
    ("word", "#2563eb", {"doc", "docx", "rtf", "odt"}),
    ("excel", "#15803d", {"xls", "xlsx", "csv", "ods"}),
    ("slides", "#ea580c", {"ppt", "pptx", "odp"}),
    ("text", "#6b7280", {"txt", "md", "log", "ini"}),
    ("code", "#0d9488", {"py", "js", "html", "css", "json", "xml", "c", "cpp", "java", "bat", "ps1", "sh"}),
    ("audio", "#c026d3", {"mp3", "wav", "flac", "ogg", "m4a"}),
    ("video", "#db2777", {"mp4", "mkv", "avi", "mov", "wmv", "webm"}),
    ("app", "#0ea5e9", {"exe", "msi"}),
)


def file_type(ext):
    for name, color, exts in TYPES:
        if ext in exts:
            return name, color
    return "file", "#64748b"


def desktop_dirs():
    """The user's Desktop plus the shared Public Desktop (what Windows merges on screen)."""
    dirs = []
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            for csidl in (0x10, 0x19):
                if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
                    dirs.append(buf.value)
        except Exception:
            pass
    if not dirs:
        h = os.path.expanduser("~")
        dirs = [os.path.join(h, "Desktop"), os.path.join(h, "OneDrive", "Desktop")]
    out, seen = [], set()
    for d in dirs:
        if os.path.isdir(d) and os.path.normcase(d) not in seen:
            seen.add(os.path.normcase(d))
            out.append(d)
    return out


def scan_desktop(dirs):
    items, seen = [], set()
    for d in dirs:
        try:
            for e in os.scandir(d):
                key = e.name.lower()
                if key in seen or key in ("desktop.ini", "thumbs.db"):
                    continue
                try:
                    st = e.stat()
                    if os.name == "nt" and getattr(st, "st_file_attributes", 0) & 0x2:
                        continue  # hidden
                    if os.name != "nt" and e.name.startswith("."):
                        continue
                    isdir = e.is_dir()
                except OSError:
                    continue
                seen.add(key)
                ext = "" if isdir else os.path.splitext(e.name)[1].lower().lstrip(".")
                label = os.path.splitext(e.name)[0] if ext in ("lnk", "url") else e.name
                items.append(dict(name=e.name, label=label, path=e.path, isdir=isdir, ext=ext,
                                  size=None if isdir else st.st_size, mtime=st.st_mtime))
        except OSError:
            pass
    items.sort(key=lambda r: (not r["isdir"], r["name"].lower()))
    return items


def wrap_label(text, font, maxw, maxlines=2):
    """Wrap like the Windows desktop: up to 2 lines, then an ellipsis."""
    lines, s = [], text
    while s and len(lines) < maxlines:
        if font.measure(s) <= maxw:
            lines.append(s)
            s = ""
            break
        n = len(s)
        while n > 1 and font.measure(s[:n]) > maxw:
            n -= 1
        cut = s.rfind(" ", 1, n + 1)
        if cut > 0:
            n = cut
        lines.append(s[:n].rstrip())
        s = s[n:].lstrip()
    if s and lines:
        last = lines[-1]
        while last and font.measure(last + "...") > maxw:
            last = last[:-1]
        lines[-1] = last + "..."
    return lines


SORTS = ("Name", "Type", "Date modified", "Size")
SIZES = {"Large": (96, 108, 1.0), "Medium": (82, 92, 0.8), "Small": (70, 80, 0.62)}


def rrect(c, x0, y0, x1, y1, r=8, **kw):
    """Rounded rectangle."""
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
           x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, **kw)


class Home(ttk.Frame):
    """Your Desktop as large icons with names underneath, like Windows."""
    PAD = 14

    def __init__(self, app, parent):
        super().__init__(parent)
        S = app.S
        self.app, self.all, self.items, self.sel, self.anchor = app, [], [], set(), None
        self.press_idx, self.band_start, self.band_base, self.moved = None, None, set(), False
        self.cols, self.sig, self.imgs, self.dirs, self._thumbs = 1, None, [], [], {}
        self.wp_off = (0, 0)
        self.sortv = tk.StringVar(value=S.get("home_sort") if S.get("home_sort") in SORTS else "Name")
        self.sizev = tk.StringVar(value=S.get("home_size") if S.get("home_size") in SIZES else "Large")
        self.q = tk.StringVar()
        self.set_size()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.title_text, self.count_text = "Desktop", ""
        self.hd = hd = tk.Canvas(self, height=56, highlightthickness=0)
        hd.grid(row=0, column=0, columnspan=2, sticky="ew")
        hd.bind("<Configure>", lambda e: app.debounce("hdr", self.paint_header))
        self.c_size = ttk.Combobox(hd, textvariable=self.sizev, values=list(SIZES), state="readonly", width=8)
        self.c_sort = ttk.Combobox(hd, textvariable=self.sortv, values=list(SORTS), state="readonly", width=13)
        self.entry = ttk.Entry(hd, textvariable=self.q, width=20)
        self.c_size.bind("<<ComboboxSelected>>", self.on_view)
        self.c_sort.bind("<<ComboboxSelected>>", self.on_view)
        self.q.trace_add("write", lambda *a: self.apply())

        self.cv = c = tk.Canvas(self, highlightthickness=0, yscrollincrement=36, takefocus=1)
        c.grid(row=1, column=0, sticky="nsew")
        self.sb = ttk.Scrollbar(self, command=c.yview)
        self.sb.grid(row=1, column=1, sticky="ns")
        c.configure(yscrollcommand=self._sbset)
        ttk.Label(self, text="Drop files here to archive them   \u00b7   drop archives to extract them",
                  style="Hint.TLabel", anchor="center").grid(row=2, column=0, columnspan=2, sticky="ew")
        c.bind("<Configure>", lambda e: app.debounce("home", self.redraw))
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Double-1>", lambda e: self.open_selected())
        bind_right_click(c, self.on_right)
        c.bind("<Motion>", self.on_hover)
        c.bind("<Leave>", lambda e: self.draw_hover(None))
        bind_wheel(c, lambda n: c.yview_scroll(n * 2, "units"))
        c.bind("<Return>", lambda e: self.open_selected())
        c.bind("<Delete>", lambda e: app.on_delete())
        c.bind("<Shift-Delete>", lambda e: app.on_delete(permanent=True))
        c.bind("<Control-a>", lambda e: self.select_all())
        c.bind("<F5>", lambda e: self.reload())
        self.after(2000, self.poll)

    def paint_header(self):
        c = self.hd
        c.update_idletasks()
        w, h = c.winfo_width(), 56
        if w < 100:
            return
        c.delete("all")
        c.configure(bg=PAL["bg"])
        img = self.app.wallpaper(c, w, h)
        if img:
            self.app.put_wallpaper(c, img, w, h)
        col = "#ffffff" if img else PAL["fg"]

        def txt(x, y, text, font, anchor="w", tag=""):
            if img:
                c.create_text(x + 1, y + 1, text=text, anchor=anchor, fill="#000000", font=font, tags=tag)
            c.create_text(x, y, text=text, anchor=anchor, fill=col, font=font, tags=tag)

        txt(20, 28, self.title_text, fnt(17, True))
        tw = tkfont.Font(font=fnt(17, True)).measure(self.title_text)
        txt(20 + tw + 14, 31, self.count_text, fnt(10), tag="count")
        f10, x = tkfont.Font(font=fnt(10)), w - 16
        for widget, label in ((self.c_size, "Icons:"), (self.c_sort, "Sort:"), (self.entry, "Search:")):
            c.create_window(x, 28, window=widget, anchor="e")
            x -= widget.winfo_reqwidth() + 8
            txt(x, 28, label, fnt(10), anchor="e")
            x -= f10.measure(label) + 18

    # ---- data
    def set_size(self):
        self.CW, self.CH, self.sc = SIZES[self.sizev.get()]
        self.font = tkfont.Font(family=FONT["family"], size=round((9 if self.sc >= 0.8 else 8) * FONT["scale"]))

    def on_view(self, _=None):
        self.app.S["home_sort"], self.app.S["home_size"] = self.sortv.get(), self.sizev.get()
        save_settings(self.app.S)
        self.set_size()
        self._thumbs.clear()
        self.apply()

    def _sbset(self, a, b):
        self.sb.set(a, b)
        if self.cv.find_withtag("wp"):          # keep the background fixed while scrolling
            self.cv.coords("wp", self.wp_off[0], self.cv.canvasy(0) + self.wp_off[1])
        if float(a) <= 0.0 and float(b) >= 1.0:
            self.sb.grid_remove()
        else:
            self.sb.grid()

    def signature(self):
        out = []
        for d in self.dirs:
            try:
                out.append((d, os.stat(d).st_mtime))
            except OSError:
                out.append((d, 0))
        return tuple(out)

    def poll(self):
        try:
            if self.app.view == "home" and self.signature() != self.sig:
                self.reload()
        finally:
            self.after(2000, self.poll)

    def reload(self):
        self.dirs = desktop_dirs()
        self.all = scan_desktop(self.dirs)
        self.sig = self.signature()
        self.apply()

    def apply(self):
        """Filter + sort the scanned items, keeping the current selection."""
        keep = {self.items[i]["path"] for i in self.sel if i < len(self.items)}
        q = self.q.get().strip().lower()
        items = [it for it in self.all if q in it["name"].lower()]
        keys = {"Name": lambda r: r["name"].lower(), "Type": lambda r: (r["ext"], r["name"].lower()),
                "Date modified": lambda r: -r["mtime"], "Size": lambda r: -(r["size"] or 0)}
        items.sort(key=keys[self.sortv.get()])
        items.sort(key=lambda r: not r["isdir"])
        self.items = items
        self.sel = {i for i, it in enumerate(items) if it["path"] in keep}
        self.redraw()
        self.notify()

    def selected_rows(self):
        return [self.items[i] for i in sorted(self.sel) if i < len(self.items)]

    def select_all(self):
        self.sel = set(range(len(self.items)))
        self.draw_sel()
        self.notify()

    def notify(self):
        n, t = len(self.sel), len(self.items)
        txt = f"{t} item(s)" + (f" of {len(self.all)}" if t != len(self.all) else "")
        self.count_text = txt + (f"   \u00b7   {n} selected" if n else "")
        if self.hd.find_withtag("count"):
            self.hd.itemconfig("count", text=self.count_text)
        if self.app.view == "home":
            self.app.set_status(f"{n} selected  /  {t} item(s) on your Desktop" if n else
                                f"Desktop  -  {t} item(s)")

    def open_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        r = rows[0]
        if r["isdir"]:
            self.app.goto(r["path"])
        elif kind(r["path"]):
            self.app.open_archive(r["path"])
        else:
            self.app.start_file(r["path"], r["name"])

    # ---- drawing
    def thumb(self, it):
        if not Image or it["ext"] not in IMG_EXT or (it["size"] or 0) > 6_000_000:
            return None
        key = (it["path"], it["mtime"])
        if key in self._thumbs:
            return self._thumbs[key]
        try:
            im = Image.open(it["path"])
            d = int(40 * self.sc)
            im.thumbnail((d, d))
            t = to_photo(im.convert("RGB"))
        except Exception:
            t = None
        if len(self._thumbs) > 200:
            self._thumbs.clear()
        self._thumbs[key] = t
        return t

    def draw_icon(self, x, y, it, i):
        c, tag = self.cv, f"ic{i}"
        poly = lambda *a, **k: c.create_polygon(*a, tags=tag, **k)
        rect = lambda *a, **k: c.create_rectangle(*a, tags=tag, **k)
        th = None if it["isdir"] else self.thumb(it)                 # pictures keep their thumbnail
        real = None if th else icon_for(it["path"], it["isdir"], max(16, int(48 * self.sc)))
        if real:                                                     # the real Windows icon
            c.create_image(x, y + 24, image=real, tags=tag)
            self.imgs.append(real)
            return
        if it["isdir"]:
            pts = (x - 22, y + 6, x - 22, y + 42, x + 22, y + 42, x + 22, y + 12, x + 2, y + 12, x - 2, y + 6)
            poly(*[v + (3 if j % 2 else 2) for j, v in enumerate(pts)], fill="#000000", stipple="gray25")
            poly(*pts, fill="#e0a30c", outline="#c78a05")
            rect(x - 22, y + 16, x + 22, y + 42, fill="#f9c846", outline="#d99a0b")
            rect(x - 21, y + 17, x + 21, y + 21, fill="#fde58f", outline="")
            rect(x - 21, y + 38, x + 21, y + 41, fill="#f0b83a", outline="")
        else:
            name, color = file_type(it["ext"])
            if th:
                rect(x - 22, y + 5, x + 24, y + 47, fill="#000000", stipple="gray25", outline="")
                rect(x - 23, y + 3, x + 23, y + 45, fill="#ffffff", outline="#9ca3af")
                c.create_image(x, y + 24, image=th, tags=tag)
                self.imgs.append(th)
            else:
                page = (x - 15, y + 2, x + 5, y + 2, x + 15, y + 12, x + 15, y + 46, x - 15, y + 46)
                poly(*[v + (3 if j % 2 else 2) for j, v in enumerate(page)], fill="#000000", stipple="gray25")
                poly(*page, fill="#ffffff", outline="#9ca3af")
                poly(x + 5, y + 2, x + 5, y + 12, x + 15, y + 12, fill="#e5e7eb", outline="#9ca3af")
                if name == "archive":
                    for j in range(5):
                        rect(x - 3 + (j % 2) * 3, y + 4 + j * 5, x + (j % 2) * 3, y + 8 + j * 5,
                             fill=color, outline="")
                if it["ext"] in ("lnk", "url"):
                    rect(x - 14, y + 33, x - 2, y + 45, fill="#ffffff", outline="#6b7280")
                    c.create_text(x - 8, y + 39, text="\u2197", fill="#2563eb",
                                  font=("Segoe UI", 9, "bold"), tags=tag)
                else:
                    rect(x - 15, y + 30, x + 15, y + 42, fill=color, outline=color)
                    c.create_text(x, y + 36, text=(it["ext"] or "FILE").upper()[:4], fill="#ffffff",
                                  font=("Segoe UI", max(5, int(7 * self.sc)), "bold"), tags=tag)
        if self.sc != 1:
            c.scale(tag, x, y, self.sc, self.sc)

    def redraw(self):
        c = self.cv
        w, vh = max(c.winfo_width(), 200), max(c.winfo_height(), 100)
        c.delete("all")
        self.imgs = []
        c.configure(bg=PAL["bg"])
        self.cols = cols = max(1, (w - 2 * self.PAD) // self.CW)
        n_rows = (len(self.items) + cols - 1) // cols
        H = max(vh, 2 * self.PAD + n_rows * self.CH)
        c.configure(scrollregion=(0, 0, w, H))
        img = self.app.wallpaper(c, w, vh)
        self.wp_off = (0, 0) if Image else (w // 2, vh // 2)
        if img:
            c.create_image(self.wp_off[0], c.canvasy(0) + self.wp_off[1], image=img, tags="wp",
                           anchor="nw" if Image else "center")
            self.imgs.append(img)
        if not self.items:
            msg = ("No matching items." if self.all else
                   "Your Desktop is empty.\nDrop files here to archive them.")
            if img:
                c.create_text(w // 2 + 1, vh // 2 + 1, text=msg, fill="#000000", font=fnt(13),
                              justify="center")
            c.create_text(w // 2, vh // 2, text=msg, fill="#ffffff" if img else PAL["muted_bg"],
                          font=fnt(13), justify="center")
        ih = int(48 * self.sc)
        for i, it in enumerate(self.items):
            x0, y0 = self.PAD + (i % cols) * self.CW, self.PAD + (i // cols) * self.CH
            cx, ty = x0 + self.CW // 2, y0 + 6 + ih + 6
            it["box"] = box = (x0 + 3, y0 + 1, x0 + self.CW - 3, ty + 2 * 15 + 5)
            if not img:
                rrect(c, *box, 9, fill=PAL["lbg"], outline=PAL["border"])
            self.draw_icon(cx, y0 + 6, it, i)
            for k, ln in enumerate(wrap_label(it["label"], self.font, self.CW - 12)):
                if img:
                    c.create_text(cx + 1, ty + k * 15 + 1, text=ln, font=self.font, fill="#000000", anchor="n")
                c.create_text(cx, ty + k * 15, text=ln, font=self.font,
                              fill="#ffffff" if img else PAL["lfg"], anchor="n")
        self.draw_sel()

    def draw_sel(self):
        c = self.cv
        c.delete("sel")
        for i in self.sel:
            if i < len(self.items) and "box" in self.items[i]:
                rrect(c, *self.items[i]["box"], 9, fill=PAL["accent"], outline=PAL["accent"],
                      stipple="gray25", width=2, tags="sel")

    def draw_hover(self, i):
        c = self.cv
        c.delete("hov")
        if i is not None and i not in self.sel and i < len(self.items) and "box" in self.items[i]:
            rrect(c, *self.items[i]["box"], 9, fill=PAL["accent"], outline=PAL["accent"],
                  stipple="gray12", tags="hov")

    # ---- mouse
    def hit(self, x, y):
        col, row = int((x - self.PAD) // self.CW), int((y - self.PAD) // self.CH)
        if col < 0 or col >= self.cols or row < 0:
            return None
        i = row * self.cols + col
        if i >= len(self.items) or "box" not in self.items[i]:
            return None
        b = self.items[i]["box"]
        return i if b[0] <= x <= b[2] and b[1] <= y <= b[3] else None

    def on_hover(self, e):
        self.draw_hover(self.hit(self.cv.canvasx(e.x), self.cv.canvasy(e.y)))

    def on_press(self, e):
        c = self.cv
        c.focus_set()
        x, y = c.canvasx(e.x), c.canvasy(e.y)
        i = self.press_idx = self.hit(x, y)
        ctrl, shift = bool(e.state & 0x4), bool(e.state & 0x1)
        self.moved = False
        if i is None:
            self.band_start, self.band_base = (x, y), (set(self.sel) if ctrl else set())
            if not ctrl:
                self.sel = set()
                self.draw_sel()
                self.notify()
            return
        self.band_start = None
        if shift and self.anchor is not None:
            a, b = sorted((self.anchor, i))
            self.sel = set(range(a, b + 1))
        elif ctrl:
            self.sel ^= {i}
            self.anchor = i
        else:
            if i not in self.sel:
                self.sel = {i}
            self.anchor = i
        self.draw_sel()
        self.notify()

    def on_drag(self, e):
        self.moved = True
        if not self.band_start:
            return
        c = self.cv
        x, y = c.canvasx(e.x), c.canvasy(e.y)
        x0, y0 = self.band_start
        c.delete("band")
        c.create_rectangle(x0, y0, x, y, outline=PAL["accent"], fill=PAL["accent"], stipple="gray25",
                           tags="band")
        l, r, t, b = min(x0, x), max(x0, x), min(y0, y), max(y0, y)
        hit = {i for i, it in enumerate(self.items) if "box" in it and it["box"][0] < r and
               it["box"][2] > l and it["box"][1] < b and it["box"][3] > t}
        self.sel = self.band_base | hit
        self.draw_sel()
        self.notify()

    def on_release(self, e):
        self.cv.delete("band")
        self.band_start = None
        i = self.press_idx
        if (i is not None and not self.moved and not (e.state & 0x5) and len(self.sel) > 1
                and i in self.sel):
            self.sel = {i}
            self.draw_sel()
            self.notify()

    def on_right(self, e):
        c = self.cv
        c.focus_set()
        i = self.hit(c.canvasx(e.x), c.canvasy(e.y))
        if i is None:
            self.sel = set()
        elif i not in self.sel:
            self.sel = {i}
        self.draw_sel()
        self.notify()
        self.app.popup_ctx(e.x_root, e.y_root)


# ================================================================== main
class Main(BaseTk):
    def __init__(self):
        # Linux menus and docks match a window to its menu entry by its class (StartupWMClass=Multizip in
        # install-linux.sh). Tk title-cases this name - Tk 9 lowercases the rest - so it must be all lowercase.
        super().__init__(**({} if os.name == "nt" else {"className": "multizip"}))
        self.S = load_settings()
        self.title(APP)
        if os.name != "nt":
            self.set_icon()
        self.geometry("1120x680")
        self.minsize(1120, 520)
        self.cwd, self.arc, self.inner = os.path.expanduser("~"), None, ""
        self.entries, self.items, self.view = [], {}, "home"
        self.sort_col, self.sort_rev, self.pwd, self.drag_tmp = "#0", False, None, None
        self.home_dragging = False
        self._tools = {}
        self.dl, self._dl_busy = None, False                       # the download engine starts when first needed
        self.addr = tk.StringVar()
        self._wp, self._src, self._fit, self._jobs, self.menus, self.btn_text = {}, (None, None), (None, None), {}, [], []
        load_user_fonts()
        self.apply_fonts()
        self._menu()
        self._build()
        self.set_palette(self.S["colors"])
        self.apply_caps()
        self.bind("<Configure>", self._root_cfg)
        for key, fn in (("n", self.on_create), ("o", self.open_dialog), ("e", self.on_extract),
                        ("t", self.on_test), ("q", self.on_quick)):
            self.bind(f"<Control-{key}>", lambda ev, f=fn: f())
        self.protocol("WM_DELETE_WINDOW", self.on_quit)
        self.bind("<F1>", lambda _: self.quick_start())
        self.bind("<BackSpace>", lambda _: self.up() if self.focus_get() is self.tree else None)
        self.bind("<F5>", lambda _: self.refresh())
        self.bind("<Control-a>", lambda _: self.select_all())
        if DND:
            for w in (self, self.tree, self.home.cv):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
            self.home.cv.drag_source_register(1, DND_FILES)
            self.home.cv.dnd_bind("<<DragInitCmd>>", self.on_home_drag)
            self.home.cv.dnd_bind("<<DragEndCmd>>", self.on_home_drag_end)
            self.tree.drag_source_register(1, DND_FILES)
            self.tree.dnd_bind("<<DragInitCmd>>", self.on_drag_init)
        self.show("home")

    # ---- construction
    def set_icon(self):
        """The window icon (taskbar / dock) from multizip.png: bundled by build.sh, or next to multizip.py."""
        for d in (getattr(sys, "_MEIPASS", ""), os.path.dirname(os.path.abspath(__file__))):
            f = os.path.join(d, "multizip.png") if d else ""
            if f and os.path.isfile(f):
                try:
                    self._icon = tk.PhotoImage(file=f)
                    self.iconphoto(True, self._icon)
                except tk.TclError:
                    pass
                return

    def _menu(self):
        bar = tk.Menu(self)
        self.config(menu=bar)

        def sub(label, items):
            m = tk.Menu(bar, tearoff=0)
            self.menus.append(m)
            for it in items:
                if it is None:
                    m.add_separator()
                else:
                    m.add_command(label=it[0], command=it[1], accelerator=it[2] if len(it) > 2 else None)
            bar.add_cascade(label=label, menu=m)

        sub("File", [("Create archive...", self.on_create, "Ctrl+N"), ("Open archive...", self.open_dialog, "Ctrl+O"),
                     ("Extract archives...", self.on_extract, "Ctrl+E"),
                     ("Open any file as an archive...", self.on_open_as_archive), None, ("Exit", self.on_quit)])
        sub("Edit", [("Select all", self.select_all, "Ctrl+A"),
                     ("Quick archive selection", self.on_quick, "Ctrl+Q"),
                     ("Add files to the open archive...", self.on_add_into)])
        sub("View", [("Home", lambda: self.show("home")), ("Browse files", lambda: self.show("files")),
                     ("Up one level", self.up, "Backspace"), ("Refresh", self.refresh, "F5"), None,
                     ("Settings: themes and backgrounds...", self.open_settings)])
        sub("Tools", [("Disk usage analyser...", self.on_disk_usage), ("Clean up assistant...", self.on_cleanup),
                    ("Format / bootable USB drive...", self.on_usb), ("Download manager...", self.on_downloads)])
        sub("Help", [("Quick start", self.quick_start, "F1"), ("Supported formats...", self.show_formats)])

    def _build(self):
        self.banner = c = tk.Canvas(self, height=112, highlightthickness=0)
        c.pack(fill="x")
        c.bind("<Configure>", lambda e: self.debounce("banner", self.paint_banner))
        self.nav, self.acts = {}, []
        for key, text, tip in (("home", "\u2302  Home", "Your Desktop"),
                               ("files", "\u2630  Browse files", "Browse folders and open archives")):
            b = ttk.Button(c, text=text, style="Nav.TButton", command=lambda k=key: self.show(k))
            Tip(b, tip)
            self.nav[key] = b
            self.btn_text.append((b, text))

        def act(text, cmd, tip, style="TButton"):
            b = ttk.Button(c, text=text, command=cmd, style=style)
            Tip(b, tip)
            self.acts.append(b)          # packed right-to-left in this order
            self.btn_text.append((b, text))

        act("\u21e9  Extract", self.on_extract, "Extract the selected archives, or choose some")
        act("\uff0b  Create archive", self.on_create, "Compress the selected files, or choose some",
            "Action.TButton")
        act("\u25d4  Disk usage", self.on_disk_usage, "See which folders and files are using the most space")
        act("\u2736  Clean up", self.on_cleanup, "Find junk files you can safely remove and free up space")
        act("\u25a3  Bootable USB", self.on_usb, "Format a USB drive or make it bootable (Windows installer, Linux)")
        act("\u2193  Downloads", self.on_downloads, "Download files from links: queue, pause, resume, several connections")
        foot = ttk.Frame(self)
        foot.pack(side="bottom", fill="x")
        self.dlstat = ttk.Label(foot, style="Status.TLabel", anchor="e", padding=(10, 5))
        self.dlstat.pack(side="right")
        self.status = ttk.Label(foot, style="Status.TLabel", anchor="w", padding=(10, 5))
        self.status.pack(side="left", fill="x", expand=True)
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        self.home = Home(self, self.body)
        self.files = ttk.Frame(self.body)

        nav = ttk.Frame(self.files, padding=(8, 2))
        nav.pack(fill="x")
        ttk.Button(nav, text="\u2191 Up", width=6, command=self.up).pack(side="left")
        self.drive_map = {}
        e = ttk.Combobox(nav, textvariable=self.addr, values=[], postcommand=self.fill_drives)
        e.pack(side="left", fill="x", expand=True, padx=6)
        e.bind("<Return>", lambda _: self.goto(self.addr.get()))
        e.bind("<<ComboboxSelected>>", self.on_pick_drive)
        self.addr_box = e
        Tip(e, "Click the arrow to pick a drive or device. You can also type or paste a folder path and press Enter.")
        self.fill_drives()
        links = ttk.Frame(self.files, padding=(8, 0, 8, 4))
        links.pack(fill="x")
        ttk.Label(links, text="Go to:").pack(side="left", padx=(0, 4))
        home = os.path.expanduser("~")
        for name in ("Desktop", "Documents", "Downloads", "Pictures"):
            target = (desktop_dirs() or [os.path.join(home, name)])[0] if name == "Desktop" \
                else os.path.join(home, name)
            ttk.Button(links, text=name, style="Link.TButton",
                       command=lambda p=target: self.goto(p)).pack(side="left", padx=2)
        ttk.Button(links, text="This PC", style="Link.TButton", command=self.go_pc).pack(side="left", padx=2)

        wrap = ttk.Frame(self.files)
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.tree = ttk.Treeview(wrap, columns=("size", "packed", "mod"), selectmode="extended")
        for c, t, w in (("#0", "Name", 400), ("size", "Size", 90), ("packed", "Packed", 90),
                        ("mod", "Modified", 150)):
            self.tree.heading(c, text=t, command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=w, anchor="w" if c in ("#0", "mod") else "e")
        sb = ttk.Scrollbar(wrap, command=self.tree.yview)
        self.tree.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.ctx = tk.Menu(self, tearoff=0)
        self.newmenu = tk.Menu(self.ctx, tearoff=0)
        self.menus.append(self.newmenu)
        for lbl, cmd in (("Folder", lambda: self.on_new(True, "New folder")),
                         ("Text document", lambda: self.on_new(False, "New Text Document.txt")),
                         ("File (choose the name and type)...", lambda: self.on_new(False, ""))):
            self.newmenu.add_command(label=lbl, command=cmd)
        bind_right_click(self.tree, self.show_ctx)
        self.tree.bind("<Double-1>", self.on_open)
        self.tree.bind("<Return>", self.on_open)
        self.tree.bind("<Delete>", lambda e: self.on_delete())
        self.tree.bind("<Shift-Delete>", lambda e: self.on_delete(permanent=True))
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

    # ---- look & feel
    def set_palette(self, t):
        global PAL
        PAL = P = palette(t)
        self.colors = t
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=P["bg"], foreground=P["fg"], fieldbackground=P["lbg"],
                     bordercolor=P["border"], lightcolor=P["bg"], darkcolor=P["bg"], troughcolor=P["btn"],
                     focuscolor=P["btn"], insertcolor=P["fg"], font=fnt(10))
        st.configure("TButton", background=P["btn"], foreground=P["fg"], padding=(10, 6), relief="flat")
        st.map("TButton", background=[("pressed", P["accent"]), ("active", P["border"])],
               foreground=[("pressed", P["accent_fg"])])
        st.configure("Accent.TButton", background=P["accent"], foreground=P["accent_fg"], padding=(12, 6),
                     font=fnt(10, True))
        st.map("Accent.TButton", background=[("pressed", P["accent_hi"]), ("active", P["accent_hi"])],
               foreground=[("active", P["accent_fg"]), ("pressed", P["accent_fg"])])
        st.configure("Link.TButton", background=P["bg"], foreground=P["accent"], padding=(6, 2), borderwidth=0)
        st.map("Link.TButton", foreground=[("active", P["fg"])], background=[("active", P["bg"])])
        st.configure("Nav.TButton", background=P["bg"], foreground=P["fg"], padding=(14, 7), borderwidth=0)
        st.map("Nav.TButton", background=[("active", P["btn"])])
        st.configure("NavActive.TButton", background=P["accent"], foreground=P["accent_fg"], padding=(14, 7),
                     borderwidth=0, font=fnt(10, True))
        st.map("NavActive.TButton", background=[("active", P["accent_hi"])],
               foreground=[("active", P["accent_fg"])])
        st.configure("Action.TButton", background=P["btn"], foreground=P["accent"], padding=(12, 7), font=fnt(10, True))
        st.map("Action.TButton", background=[("pressed", P["accent"]), ("active", P["border"])],
               foreground=[("pressed", P["accent_fg"])])
        st.configure("Title.TLabel", font=fnt(17, True))
        st.configure("Sub.TLabel", foreground=P["muted_bg"])
        st.configure("Hint.TLabel", background=P["hdr"], foreground=P["muted_bg"], padding=(10, 7))
        st.configure("Treeview", background=P["lbg"], fieldbackground=P["lbg"], foreground=P["lfg"],
                     rowheight=27, borderwidth=0)
        st.map("Treeview", background=[("selected", P["accent"])], foreground=[("selected", P["accent_fg"])])
        st.configure("Treeview.Heading", background=P["hdr"], foreground=P["fg"], relief="flat",
                     padding=(8, 6), font=fnt(10, True))
        st.map("Treeview.Heading", background=[("active", P["btn"])])
        st.configure("TEntry", fieldbackground=P["lbg"], foreground=P["lfg"], padding=4)
        st.configure("TCombobox", fieldbackground=P["lbg"], foreground=P["lfg"], background=P["btn"],
                     arrowcolor=P["fg"], padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", P["lbg"])], foreground=[("readonly", P["lfg"])],
               selectbackground=[("readonly", P["lbg"])], selectforeground=[("readonly", P["lfg"])])
        st.configure("TProgressbar", background=P["accent"], troughcolor=P["btn"], bordercolor=P["btn"],
                     lightcolor=P["accent"], darkcolor=P["accent"])
        st.configure("TScrollbar", background=P["btn"], troughcolor=P["bg"], arrowcolor=P["fg"],
                     bordercolor=P["bg"])
        st.configure("TCheckbutton", background=P["bg"], foreground=P["fg"])
        st.map("TCheckbutton", background=[("active", P["bg"])],
               indicatorcolor=[("selected", P["accent"]), ("!selected", P["lbg"])])
        st.configure("TNotebook", background=P["bg"], borderwidth=0)
        st.configure("TNotebook.Tab", background=P["btn"], foreground=P["fg"], padding=(14, 6))
        st.map("TNotebook.Tab", background=[("selected", P["bg"])])
        st.configure("TSeparator", background=P["border"])
        st.configure("Status.TLabel", background=P["hdr"], foreground=P["fg"])
        for opt, val in (("background", P["lbg"]), ("foreground", P["lfg"]),
                         ("selectBackground", P["accent"]), ("selectForeground", P["accent_fg"])):
            self.option_add(f"*TCombobox*Listbox.{opt}", val)
        self._lbfont = tkfont.Font(family=FONT["family"], size=round(10 * FONT["scale"]))
        self.option_add("*TCombobox*Listbox.font", self._lbfont)
        self.configure(bg=P["bg"])
        for m in self.menus + [self.ctx]:
            m.configure(bg=P["lbg"], fg=P["lfg"], activebackground=P["accent"],
                        activeforeground=P["accent_fg"], bd=0, font=fnt(10))
        self.tree.tag_configure("odd", background=P["zebra"])
        self.tree.tag_configure("even", background=P["lbg"])
        self.redraw_backgrounds()

    def apply_fonts(self):
        FONT["family"] = resolve_font(self.S.get("font"))
        try:
            FONT["scale"] = float(self.S.get("font_scale", 1.0))
        except (TypeError, ValueError):
            FONT["scale"] = 1.0
        for n in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                  "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(n).configure(family=FONT["family"], size=round(10 * FONT["scale"]))
            except tk.TclError:
                pass

    def refresh_fonts(self):
        self.apply_fonts()
        self.set_palette(self.colors)
        self.home.set_size()
        self.home.apply()
        self.apply_caps()

    def apply_caps(self):
        caps = bool(self.S.get("caps"))
        for b, t in self.btn_text:
            b.configure(text=t.upper() if caps else t)
        self.home.title_text = "DESKTOP" if caps else "Desktop"
        self.paint_banner()
        self.home.paint_header()

    def debounce(self, key, fn, ms=60):
        if self._jobs.get(key):
            self.after_cancel(self._jobs[key])
        self._jobs[key] = self.after(ms, fn)

    def wallpaper(self, widget, w, h):
        """The background, scaled to the WHOLE window, cut to the area `widget` covers.
        The banner and Home each show their own slice of the same fitted picture, so it
        always lines up and re-scales when the window is resized or maximised."""
        p = self.S.get("bg_image")
        if not p or not os.path.isfile(p) or w < 20 or h < 20:
            return None
        if not Image:                      # no Pillow: show the picture unscaled
            if ("raw", p) not in self._wp:
                try:
                    self._wp[("raw", p)] = tk.PhotoImage(file=p)
                except Exception:
                    return None
            return self._wp[("raw", p)]
        try:
            x = max(0, widget.winfo_rootx() - self.winfo_rootx())
            y = max(0, widget.winfo_rooty() - self.winfo_rooty())
            W, H = max(self.winfo_width(), x + w), max(self.winfo_height(), y + h)
            mode = self.S.get("bg_fit") if self.S.get("bg_fit") in BG_FITS else "Fill"
            dim = int(self.S.get("bg_dim", 0))
            fkey = (p, W, H, mode, dim, PAL["bg"])
            if self._fit[0] != fkey:
                if self._src[0] != p:
                    src = Image.open(p).convert("RGB")
                    src.thumbnail((2560, 2560))
                    self._src = (p, src)
                src = self._src[1]
                if mode == "Stretch":
                    im = src.resize((W, H), RESAMPLE)
                else:
                    r = (min if mode == "Fit" else max)(W / src.width, H / src.height)
                    im = src.resize((max(1, int(src.width * r) + (mode == "Fill")),
                                     max(1, int(src.height * r) + (mode == "Fill"))), RESAMPLE)
                if dim:
                    im = Image.blend(im, Image.new("RGB", im.size, "#000000"), dim / 100)
                full = Image.new("RGB", (W, H), PAL["bg"])      # letterbox colour for "Fit"
                full.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
                self._fit = (fkey, full)
            rkey = (fkey, x, y, w, h)
            if rkey not in self._wp:
                if len(self._wp) > 12:
                    self._wp.clear()
                self._wp[rkey] = to_photo(self._fit[1].crop((x, y, x + w, y + h)))
            return self._wp[rkey]
        except Exception:
            return None

    def redraw_backgrounds(self):
        self._wp.clear()
        self._src, self._fit = (None, None), (None, None)
        self.paint_banner()
        self.home.paint_header()
        self.home.redraw()

    def put_wallpaper(self, c, img, w, h):
        if Image:
            c.create_image(0, 0, image=img, anchor="nw")
        else:
            c.create_image(w // 2, h // 2, image=img, anchor="center")
        c.img = img

    def paint_banner(self):
        """Top panel: the title strip and the toolbar, both drawn over the background picture."""
        c, h = self.banner, 112
        c.update_idletasks()
        w = c.winfo_width()
        if w < 50:
            return
        c.delete("all")
        c.configure(bg=PAL["bg"])
        img = self.wallpaper(c, w, h)
        if img:
            self.put_wallpaper(c, img, w, h)
        else:
            c.create_rectangle(0, 0, w, 60, fill=PAL["accent"], outline="")
        fg = "#ffffff" if (img or lum(PAL["accent"]) < .6) else "#111111"
        name = "MULTIZIP" if self.S.get("caps") else "MultiZip"
        if img:
            c.create_text(21, 31, text=name, anchor="w", fill="#000000", font=fnt(20, True))
        c.create_text(20, 30, text=name, anchor="w", fill=fg, font=fnt(20, True))
        c.create_text(w - 16, 30, text="zip  \u00b7  7z  \u00b7  rar  \u00b7  tar", anchor="e", fill=fg,
                      font=fnt(10))
        x = 12
        for k in ("home", "files"):
            c.create_window(x, 86, window=self.nav[k], anchor="w")
            x += self.nav[k].winfo_reqwidth() + 4
        x = w - 12
        for b in self.acts:
            c.create_window(x, 86, window=b, anchor="e")
            x -= b.winfo_reqwidth() + 4

    def repaint_all(self):
        self.paint_banner()
        if self.view == "home":
            self.home.paint_header()
            self.home.redraw()

    def _root_cfg(self, e):
        # A size change of the WINDOW (even height only) changes the fitted picture for every area.
        if e.widget is self:
            self.debounce("bg", self.repaint_all, 90)

    def open_tool(self, cls, *args):
        w = self._tools.get(cls)
        if w is not None and w.winfo_exists():                     # already open: bring it forward
            w.deiconify()
            w.lift()
            w.focus_force()
            return
        self._tools[cls] = cls(self, *args)

    def on_disk_usage(self):
        if self.view == "files" and not self.arc and self.cwd and os.path.isdir(self.cwd):
            start = self.cwd
        else:
            start = (os.environ.get("SystemDrive", "C:") + "\\") if os.name == "nt" else "/"
        self.open_tool(DiskUsageDialog, start)

    def on_cleanup(self):
        self.open_tool(CleanupDialog)

    def on_usb(self):
        self.open_tool(UsbDialog)

    def get_dl(self):
        if self.dl is None:
            self.dl = Downloader()
            self.dl.max_active, self.dl.throttle.rate = self.S["dl_active"], self.S["dl_limit"] * 1024
            self.dl_tick()
        return self.dl

    def on_downloads(self):
        self.get_dl().schedule()
        self.open_tool(DownloadsDialog)

    def dl_tick(self):
        """Once a second: tell the user about finished downloads and show the speed in the corner."""
        self.after(1000, self.dl_tick)
        dl = self.dl
        if self._dl_busy:                                         # an extraction window is open: wait
            return
        n, q, speed = dl.summary()
        self.dlstat.config(text=f"\u2193 {n} downloading  {human(speed)}/s  " if n else
                           (f"\u2193 {q} waiting  " if q else ""))
        while True:
            try:
                ev, iid = dl.events.get_nowait()
            except queue.Empty:
                return
            v = next((x for x in dl.snapshot() if x["id"] == iid), None)
            if not v:
                continue
            if ev == "error":
                self.set_status(f"Download failed: {v['name']} - {v['error']}")
            else:
                self.set_status(f"Download finished: {v['name']}")
                if self.S["dl_extract"] and kind(v["path"]):
                    self.dl_extract(v["path"], True)

    def dl_extract(self, path, sub=True):
        """Extract a downloaded archive (into a folder named after it when `sub`)."""
        if self._dl_busy:
            return
        self._dl_busy = True
        try:
            extract_many(self, [path], None, sub, None)
        finally:
            self._dl_busy = False

    def on_quit(self):
        if self.dl is not None:
            if self.dl.running() and not messagebox.askyesno(
                    APP, "Downloads are still running.\n\nPause them and close MultiZip? You can resume them "
                         "from the download manager next time.", parent=self):
                return
            self.dl.shutdown()
        self.destroy()

    def open_settings(self):
        d = SettingsDialog(self)
        self.wait_window(d)
        save_settings(self.S)

    def set_status(self, text):
        self.status.config(text="  " + text)

    def show(self, v):
        self.view = v
        for k, b in self.nav.items():
            b.configure(style="NavActive.TButton" if k == v else "Nav.TButton")
        self.home.pack_forget()
        self.files.pack_forget()
        if v == "home":
            self.home.pack(fill="both", expand=True)
            self.update_idletasks()
            self.home.paint_header()
            self.set_status("Ready - drop files or archives anywhere in this window.")
            self.home.reload()
        else:
            self.files.pack(fill="both", expand=True)
            self.refresh()

    def quick_start(self):
        messagebox.showinfo(APP, "Quick start\n\n"
                            "Home shows your Desktop. Click files to select them (Ctrl/Shift or drag a box\n"
                            "to pick several), then press Create archive. Double-click to open.\n\n"
                            "Create an archive:  drag files or folders onto the window (or click\n"
                            "Create archive), pick a name, and press the button.\n\n"
                            "Extract:  drop one or more archives onto the window, or click Extract.\n\n"
                            "Browse:  double-click an archive to open it like a folder. Drag files out\n"
                            "of it, or drop files in to add them.\n\n"
                            "Right-click anywhere to create a new folder or file, or to delete items\n"
                            "(to the Recycle Bin, or permanently). Right-click files for more actions.\n"
                            "Disk usage shows which folders and files take up the most space.\n"
                            "Clean up finds junk (temp files, caches, the Recycle Bin) and frees space\n"
                            "only after you confirm.\n"
                            "Downloads fetches files from links: paste them, and big files download over\n"
                            "several connections. You can pause, resume and set a speed limit.\n\n"
                            "View > Settings changes themes and the background picture.")

    # ---- listing
    def rows(self):
        rows = []
        if self.arc:
            kids = {}
            for n, size, packed, mt, isdir in self.entries:
                if not n.startswith(self.inner) or n == self.inner.rstrip("/"):
                    continue
                head, _, tail = n[len(self.inner):].partition("/")
                d = kids.setdefault(head, dict(name=head, isdir=bool(tail) or isdir, size=0,
                                               packed=0, mtime=mt, path=self.inner + head))
                if tail or not isdir:
                    d["size"] += size or 0
                    d["packed"] += packed or 0
            return list(kids.values())
        if self.cwd == "":
            disks = ([c + ":\\" for c in string.ascii_uppercase if os.path.exists(c + ":\\")] if os.name == "nt"
                     else [p for _t, p in list_drives() if p])         # macOS / Linux: / plus mounted disks
            return [dict(name=p, isdir=True, size=None, packed=None, mtime=None, path=p) for p in disks]
        try:
            for e in os.scandir(self.cwd):
                try:
                    st = e.stat()
                    d = e.is_dir()
                    rows.append(dict(name=e.name, isdir=d, size=None if d else st.st_size, packed=None,
                                     mtime=datetime.fromtimestamp(st.st_mtime), path=e.path))
                except OSError:
                    pass
        except OSError as ex:
            messagebox.showerror(APP, str(ex))
        return rows

    def refresh(self):
        if self.view == "home":
            self.home.reload()
            return
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.addr.set(self.arc + os.sep + self.inner.replace("/", os.sep) if self.arc else (self.cwd or "This PC"))
        keys = {"#0": lambda r: r["name"].lower(), "size": lambda r: r["size"] or 0,
                "packed": lambda r: r["packed"] or 0, "mod": lambda r: r["mtime"] or datetime.min}
        rows = sorted(self.rows(), key=keys[self.sort_col], reverse=self.sort_rev)
        rows.sort(key=lambda r: not r["isdir"])
        for i, r in enumerate(rows):
            mt = r["mtime"].strftime("%Y-%m-%d %H:%M") if r["mtime"] else ""
            if self.arc:                                     # inside an archive: the icon for the file type
                ic = icon_for("folder", True) if r["isdir"] else icon_for("x" + os.path.splitext(r["name"])[1])
            else:
                ic = icon_for(r["path"], r["isdir"])
            iid = self.tree.insert("", "end", tags=("odd" if i % 2 else "even",), image=ic or "",
                                   text=r["name"] if ic else ("\U0001F4C1 " if r["isdir"] else "    ") + r["name"],
                                   values=(human(r["size"]) if not r["isdir"] or self.arc else "",
                                           human(r["packed"]) if self.arc else "", mt))
            self.items[iid] = r
        self.on_select()

    def on_select(self, _=None):
        if self.view == "files":
            n, tot = len(self.tree.selection()), len(self.items)
            self.set_status(f"{n} selected  /  {tot} item(s)" + ("      |  drop files here" if DND else ""))

    def selected(self):
        if self.view == "home":
            return self.home.selected_rows()
        return [self.items[i] for i in self.tree.selection() if i in self.items]

    def select_all(self):
        f = self.focus_get()
        if f is self.tree:
            self.tree.selection_set(self.tree.get_children())
        elif f is self.home.cv:
            self.home.select_all()

    def sort_by(self, col):
        self.sort_rev = (not self.sort_rev) if col == self.sort_col else False
        self.sort_col = col
        self.refresh()

    def show_ctx(self, e):
        i = self.tree.identify_row(e.y)
        if i and i not in self.tree.selection():
            self.tree.selection_set(i)
        self.popup_ctx(e.x_root, e.y_root)

    # ---- right-click menu
    def can_create_here(self):
        if self.view == "home":
            return bool(desktop_dirs())
        if self.arc:
            return kind(self.arc) == "zip"
        return self.cwd != ""

    def popup_ctx(self, x, y):
        m, sel = self.ctx, self.selected()
        m.delete(0, "end")
        in_arc = self.view == "files" and bool(self.arc)
        arcs = [r for r in sel if not r["isdir"] and kind(r["path"])] if not in_arc else []

        def add(label, cmd, enabled=True):
            m.add_command(label=label, command=cmd, state="normal" if enabled else "disabled")

        if sel:
            add("Open", self.on_open)
            if not in_arc and len(sel) == 1 and not sel[0]["isdir"] and not kind(sel[0]["path"]):
                add("Open as archive", self.on_open_as_archive)
            m.add_separator()
        m.add_cascade(label="New", menu=self.newmenu,
                      state="normal" if self.can_create_here() else "disabled")
        m.add_separator()
        if in_arc:
            add("Extract...", self.on_extract)
            add("Test archive", self.on_test)
        elif arcs:
            add("Extract...", self.on_extract)
            add("Extract to its own folder", self.on_quick_extract)
            add("Test", self.on_test)
        if sel and not in_arc:
            add("Create archive from selection...", self.on_create)
            add("Quick archive (one click)", self.on_quick)
        if in_arc or arcs or sel:
            m.add_separator()
        can_del = bool(sel) and (not in_arc or kind(self.arc) == "zip")
        if in_arc:
            add("Delete from archive", lambda: self.on_delete(), can_del)
        else:
            add("Delete  (to Recycle Bin)" if os.name == "nt" else "Delete", lambda: self.on_delete(), can_del)
            add("Delete permanently", lambda: self.on_delete(permanent=True), can_del)
        m.add_separator()
        add("Refresh", self.refresh)
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    # ---- navigation
    def fill_drives(self):
        """Refresh the drop-down each time it opens, so a USB stick you just plugged in shows up."""
        items = list_drives()
        self.drive_map = dict(items)
        self.addr_box.configure(values=[t for t, _ in items])

    def on_pick_drive(self, _=None):
        label = self.addr.get()
        if label not in self.drive_map:
            return
        path = self.drive_map[label]
        if path == "":
            return self.go_pc()
        if not os.path.isdir(path):
            messagebox.showinfo(APP, f"{path} isn't ready.\nIs a disc or memory card inserted?")
            return self.refresh()
        self.goto(path)

    def goto(self, path):
        path = path.strip().strip('"')
        if path in self.drive_map:                       # a menu entry that was typed or pasted
            path = self.drive_map[path]
        if path == "" or path.lower() in ("this pc", "computer", "my computer"):
            return self.go_pc()
        if os.path.isdir(path):
            self.arc, self.cwd = None, path
            self.show("files")
        elif os.path.isfile(path) and kind(path):
            self.open_archive(path)

    def go_pc(self):
        self.arc, self.cwd = None, ""
        self.show("files")

    def open_archive(self, p, pwd=None):
        try:
            self.entries = list_archive(p, pwd)
        except Exception as e:
            if not pwd and "password" in str(e).lower():
                pw = simpledialog.askstring(APP, "Enter password:", show="*", parent=self)
                if pw:
                    self.open_archive(p, pw)
                return
            messagebox.showerror(APP, f"Cannot open archive:\n{e}")
            return
        self.pwd = pwd
        self.arc, self.inner, self.cwd = p, "", os.path.dirname(p)
        self.show("files")

    def open_dialog(self):
        f = filedialog.askopenfilename(title="Open archive", filetypes=[
            ("Archives", " ".join("*" + e for e in ALL)), ("All files", "*.*")])
        if f:
            self.open_archive(f)

    def up(self):
        if self.view != "files":
            return
        if self.arc:
            if self.inner:
                self.inner = self.inner.rstrip("/").rpartition("/")[0]
                self.inner += "/" if self.inner else ""
            else:
                self.arc = None
        else:
            parent = os.path.dirname(self.cwd.rstrip("\\/"))
            self.cwd = parent if parent and parent != self.cwd.rstrip("\\/") else ""
        self.refresh()

    def on_open(self, _=None):
        if self.view == "home":
            self.home.open_selected()
            return
        sel = self.selected()
        if not sel:
            return
        r = sel[0]
        if self.arc:
            if r["isdir"]:
                self.inner = r["path"] + "/"
                self.refresh()
            else:
                tmp = tempfile.mkdtemp(prefix="multizip_")
                err = run_job(self, "Opening", 1, lambda t, c: extract(
                    self.arc, tmp, self.pwd, [r["path"]], t, c))
                if err:
                    messagebox.showerror(APP, err)
                else:
                    self.start_file(extracted_path(tmp, r["path"]), r["name"])
        elif r["isdir"]:
            self.cwd = r["path"]
            self.refresh()
        elif kind(r["path"]):
            self.open_archive(r["path"])
        else:
            self.start_file(r["path"], r["name"])

    def start_file(self, path, name):
        """Open a file with its Windows program, saying so when that fails."""
        try:
            if not path:
                raise FileNotFoundError("It could not be unpacked.")
            open_path(path)
        except OSError as e:
            messagebox.showerror(APP, f"Could not open {name}:\n{e}", parent=self)

    # ---- actions
    def fs_selection(self):
        if self.view == "home" or not self.arc:
            return [r["path"] for r in self.selected()]
        return []

    def fs_archives(self):
        return [p for p in self.fs_selection() if os.path.isfile(p) and kind(p)]

    def pick_archives(self):
        f = filedialog.askopenfilenames(title="Choose archives", filetypes=[
            ("Archives", " ".join("*" + e for e in ALL)), ("All files", "*.*")])
        return [p for p in f if kind(p)]

    def extract_dialog(self, archives, members=None):
        d = ExtractDialog(self, os.path.dirname(archives[0]), len(archives) > 1, self.S)
        self.wait_window(d)
        if not d.result:
            return
        dest, pwd, sub, reveal = d.result
        pw = pwd or (self.pwd if self.arc in archives else None) or None
        if extract_many(self, archives, dest or None, sub, pw, members):
            target = dest or os.path.dirname(archives[0])
            if sub and len(archives) == 1:
                target = os.path.join(target, stem(archives[0]))
            self.set_status(f"Extracted {len(archives)} archive(s) to {target}")
            if reveal:
                open_folder(target)
        self.S["reveal"] = reveal
        save_settings(self.S)
        if self.winfo_viewable():
            self.refresh()

    def on_extract(self):
        if self.view == "files" and self.arc:
            self.extract_dialog([self.arc], [r["path"] for r in self.selected()] or None)
            return
        a = self.fs_archives() or self.pick_archives()
        if a:
            self.extract_dialog(a)

    def on_quick_extract(self):
        a = self.fs_archives()
        if not a:
            return messagebox.showinfo(APP, "Select one or more archives first.")
        if extract_many(self, a, None, True, None):
            self.set_status(f"Extracted {len(a)} archive(s) next to the originals.")
        self.refresh()

    def on_test(self):
        a = [self.arc] if (self.view == "files" and self.arc) else (self.fs_archives() or self.pick_archives())
        if a:
            extract_many(self, a, None, False, self.pwd if self.arc else None, test=True)

    def on_create(self):
        out = add_to_archive(self, self.fs_selection(), self.S)
        if out:
            self.set_status(f"Created {os.path.basename(out)}  ({human(os.path.getsize(out))})")
            self.refresh()

    def on_quick(self):
        sel = self.fs_selection()
        if not sel:
            return messagebox.showinfo(APP, "Click the files or folders you want to archive first.")
        out = quick_archive(self, sel, self.S)
        if out:
            self.set_status(f"Created {os.path.basename(out)}  ({human(os.path.getsize(out))})")
            self.refresh()

    def on_add_into(self):
        if not (self.view == "files" and self.arc):
            return messagebox.showinfo(APP, "Open an archive first, then use this to add files to it.")
        f = filedialog.askopenfilenames(title="Add files to this archive")
        if f:
            self.drop_into_archive(list(f))

    def reload_archive(self):
        try:
            self.entries = list_archive(self.arc, self.pwd)
        except Exception as e:
            messagebox.showerror(APP, str(e))
        self.refresh()

    def drop_into_archive(self, paths):
        k = kind(self.arc)
        if not (k in ("zip", "7z") or self.arc.lower().endswith(".tar")):
            messagebox.showinfo(APP, "Adding files into this archive type isn't supported "
                                     "(works for .zip, .7z and plain .tar).")
            return
        if k == "7z" and not py7zr:
            messagebox.showinfo(APP, NO_7Z_ADD)
            return
        if not messagebox.askyesno(APP, f"Add {len(paths)} item(s) to\n{self.arc} ?"):
            return
        total = len(paths) if k == "7z" else len(gather(paths))
        err = run_job(self, "Adding", total, lambda t, c: add_into(
            self.arc, paths, self.inner, self.pwd, t, c))
        if err and err != "Cancelled":
            messagebox.showerror(APP, err)
        self.reload_archive()

    def on_home_drag(self, event):
        rows = self.home.selected_rows()
        if self.home.press_idx is None or not rows:
            return "refuse_drop"
        self.home_dragging = True
        return ("copy", DND_FILES, tuple(r["path"] for r in rows))

    def on_home_drag_end(self, event):
        self.home_dragging = False
        return getattr(event, "action", None)

    def on_drop(self, event):
        if self.home_dragging:
            return event.action
        paths = [os.path.abspath(p) for p in self.tk.splitlist(event.data) if os.path.exists(p)]
        if self.drag_tmp:
            paths = [p for p in paths if not p.startswith(self.drag_tmp)]
        if not paths:
            return event.action
        arcs = [p for p in paths if os.path.isfile(p) and kind(p)]
        if self.arc and self.view == "files":
            self.drop_into_archive(paths)
        elif len(paths) == 1 and len(arcs) == 1:
            self.open_archive(arcs[0])
        elif len(arcs) == len(paths):
            self.extract_dialog(arcs)
        else:
            out = add_to_archive(self, paths, self.S)
            if out:
                self.set_status(f"Created {os.path.basename(out)}  ({human(os.path.getsize(out))})")
                self.refresh()
        return event.action

    def on_drag_init(self, event):
        sel = self.selected()
        if not (self.arc and self.view == "files") or not sel:
            return "refuse_drop"
        if self.drag_tmp:                                # the previous drag's copy is no longer needed
            shutil.rmtree(self.drag_tmp, ignore_errors=True)
        self.drag_tmp = tempfile.mkdtemp(prefix="multizip_drag_")
        try:
            extract(self.arc, self.drag_tmp, self.pwd, [r["path"] for r in sel],
                    lambda n=1, label=None: None, lambda: False)
        except Exception as e:
            messagebox.showerror(APP, str(e))
            return "refuse_drop"
        paths = [f for f in (extracted_path(self.drag_tmp, r["path"]) for r in sel) if f]
        return ("copy", DND_FILES, tuple(paths)) if paths else "refuse_drop"

    def select_path(self, path):
        """Highlight an item (by full path) after the list has been refreshed."""
        if self.view == "home":
            for i, r in enumerate(self.home.items):
                if os.path.normcase(r["path"]) == os.path.normcase(path):
                    self.home.sel, self.home.anchor = {i}, i
                    self.home.draw_sel()
                    self.home.notify()
                    y = self.home.items[i].get("box", (0, 0, 0, 0))[1]
                    self.home.cv.yview_moveto(max(0, y - 40) / max(1, float(self.home.cv.cget("scrollregion").split()[3])))
                    return
        else:
            for iid, r in self.items.items():
                if r["path"] == path or os.path.normcase(r["path"]) == os.path.normcase(path):
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
                    return

    def show_formats(self):
        d = FormatsDialog(self)
        self.wait_window(d)

    def get_engine(self):
        """Download the 7-Zip engine. Returns True when it is ready."""
        if os.name != "nt":
            messagebox.showinfo(APP, "Install 7-Zip with your package manager, then restart MultiZip:\n"
                                     "  sudo apt install 7zip      (or p7zip-full)")
            return bool(engine_path(refresh=True))
        if engine_path(refresh=True):
            return True
        if not messagebox.askyesno(APP, "Download the free 7-Zip engine from 7-zip.org (about 2 MB)\n"
                                        "and add it to MultiZip?\n\nIt unlocks RAR, ISO, CAB, WIM, VHD, DMG, "
                                        "ARJ, LZH, RPM, DEB, encrypted ZIP and many more.", parent=self):
            return False
        err = run_job(self, "Getting the 7-Zip engine", 3, lambda t, c: download_engine(t, c))
        if err and err != "Cancelled":
            messagebox.showerror(APP, err, parent=self)
        ok = bool(engine_path(refresh=True))
        if ok:
            messagebox.showinfo(APP, "The 7-Zip engine is ready. MultiZip can now open many more formats.",
                                parent=self)
            self.refresh()
        return ok

    def on_open_as_archive(self, path=None):
        """Try to open ANY file as an archive (installers, renamed archives, disk images ...)."""
        if not isinstance(path, str):
            sel = [r for r in self.selected() if not r["isdir"]] if self.view != "files" or not self.arc else []
            path = sel[0]["path"] if sel else filedialog.askopenfilename(title="Open any file as an archive")
        if not path:
            return
        path = os.path.abspath(path)
        k = sniff_kind(path)
        if k == "engine" and not engine_path():
            if not messagebox.askyesno(APP, "Opening this kind of file as an archive needs the 7-Zip engine.\n\n"
                                            "Get it now (free, about 2 MB)?", parent=self):
                return
            if not self.get_engine():
                return
        FORCED[path] = k
        self.open_archive(path)
        if self.arc != path:
            FORCED.pop(path, None)

    def on_new(self, is_dir, default):
        """Right-click > New: create a folder or file where you are (Desktop on Home)."""
        if not self.can_create_here():
            return messagebox.showinfo(APP, "Open a folder first - you can't create items here.")
        in_arc = self.view == "files" and bool(self.arc)
        if self.view == "home":
            folder = desktop_dirs()[0]
        elif in_arc:
            folder = None
        else:
            folder = self.cwd
        if in_arc and kind(self.arc) != "zip":
            return messagebox.showinfo(APP, "Creating items inside an archive works for .zip files only.")
        where = "this archive" if in_arc else (folder if len(folder) < 48 else "..." + folder[-45:])
        label = "folder" if is_dir else "file"
        if not default:                                  # custom file: ask for the full name incl. extension
            hint = "Type a name with its extension, e.g.  notes.txt  or  data.csv"
        else:
            hint = f"Name for the new {label}"
        name = simpledialog.askstring(APP, f"{hint}\n(in {where})", initialvalue=default, parent=self)
        if name is None:
            return
        try:
            if in_arc:
                zip_add_empty(self.arc, self.inner, name, is_dir)
                path = self.inner + name.strip()
                self.entries = list_archive(self.arc, self.pwd)
            else:
                path = create_item(folder, name, is_dir)
        except FileExistsError as e:
            return messagebox.showerror(APP, str(e), parent=self)
        except (ValueError, OSError) as e:
            return messagebox.showerror(APP, f"Could not create it:\n{e}", parent=self)
        self.refresh()
        self.select_path(path)
        self.set_status(f"Created {label} '{name.strip()}'")

    def on_delete(self, permanent=False):
        sel = self.selected()
        if not sel:
            return messagebox.showinfo(APP, "Click the item(s) you want to delete first.")
        n = len(sel)
        what = f'"{sel[0]["name"]}"' if n == 1 else f"{n} items"
        in_arc = self.view == "files" and bool(self.arc)
        try:
            if in_arc:
                if kind(self.arc) != "zip":
                    return messagebox.showinfo(APP, "Deleting inside archives works for .zip files only.")
                if not messagebox.askyesno(APP, f"Delete {what} from the archive?\nThis cannot be undone."):
                    return
                zip_delete(self.arc, [r["path"] for r in sel])
                self.entries = list_archive(self.arc, self.pwd)
                self.refresh()
                return self.set_status(f"Deleted {what} from the archive")
            for r in sel:
                why = protected_reason(r["path"])
                if why:
                    return messagebox.showerror(APP, f'MultiZip will not delete "{r["name"]}".\n{why}')
            to_bin = not permanent and os.name == "nt"
            if to_bin:
                ok = messagebox.askyesno(APP, f"Move {what} to the Recycle Bin?")
            else:
                ok = messagebox.askyesno(APP, f"PERMANENTLY delete {what}?\n\nThis cannot be undone and "
                                              f"the {'items skip' if n > 1 else 'item skips'} the Recycle Bin.",
                                         icon="warning")
            if not ok:
                return
            paths = [r["path"] for r in sel]
            if to_bin:
                if not recycle(paths):
                    if not messagebox.askyesno(APP, "Windows could not move these items to the Recycle Bin "
                                                    "(for example, they may be on a network drive).\n\n"
                                                    "Delete them permanently instead?", icon="warning"):
                        return
                    to_bin = False
            if not to_bin:
                failed = []
                for p in paths:
                    try:
                        delete_permanently(p)
                    except OSError as e:
                        failed.append(f"{os.path.basename(p)}: {e}")
                if failed:
                    messagebox.showerror(APP, "Some items could not be deleted (they may be in use):\n\n"
                                              + "\n".join(failed[:8]))
            self.set_status(f"{'Moved to the Recycle Bin' if to_bin else 'Deleted'}: {what}")
        except Exception as e:
            messagebox.showerror(APP, str(e))
        self.refresh()


# ================================================== Linux desktop integration
# install-linux.sh runs "MultiZip --linux-integration install" (and "... remove" to undo it). From source:
#   python3 multizip.py --linux-integration install
LINUX_MIME = (
    "application/zip", "application/x-7z-compressed", "application/vnd.rar", "application/x-rar", "application/x-tar",
    "application/x-compressed-tar", "application/x-bzip-compressed-tar", "application/x-bzip2-compressed-tar",
    "application/x-xz-compressed-tar", "application/x-zstd-compressed-tar", "application/gzip", "application/x-bzip2",
    "application/x-bzip", "application/x-xz", "application/x-lzma", "application/zstd", "application/x-iso9660-image",
    "application/x-cd-image", "application/vnd.ms-cab-compressed", "application/x-rpm",
    "application/vnd.debian.binary-package", "application/x-apple-diskimage", "application/x-cpio", "application/x-arj",
    "application/x-lha")
# the same entries as the Windows right-click menu (installer.iss); "a" works on any file or folder
LINUX_VERBS = (("xh", "Extract Here"), ("x", "Extract to folder"), ("xd", "Extract files..."),
               ("t", "Test archive"), ("a", "Add to archive..."))
FM_MARK = "multizip-"                           # ids and file names of everything this adds


def _desktop_quote(arg):
    """One Exec argument as desktop entries, Dolphin, Thunar and Nemo read it (% doubled: it starts field codes)."""
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg).replace("%", "%%") + '"'


def _shell_quote(arg):
    return "'" + arg.replace("'", "'\\''") + "'"


def _app_command():
    """How to start MultiZip: the built program itself, or Python plus this script when run from source."""
    return [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]


def _write_file(path, text, mode):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = temp_beside(path)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


SCRIPT_MENU = """#!/bin/sh
# MultiZip: @NAME@  (added by MultiZip's Linux installer)
paths="${NAUTILUS_SCRIPT_SELECTED_FILE_PATHS:-$CAJA_SCRIPT_SELECTED_FILE_PATHS}"
if [ -n "$paths" ]; then
    IFS='
'
    set -f
    exec @CMD@ @VERB@ $paths
fi
exec @CMD@ @VERB@ "$@"
"""


def _thunar_actions(uca, cmd, install):
    """Add or remove MultiZip's custom actions in Thunar's uca.xml, keeping everyone else's."""
    import xml.etree.ElementTree as ET
    if os.path.isfile(uca):
        try:
            root = ET.parse(uca).getroot()
        except (ET.ParseError, OSError):
            root = None
        if root is None or root.tag != "actions":
            return "Thunar: its custom actions file could not be read, so it was left alone"
    elif not install:
        return ""
    else:
        root = ET.Element("actions")
    ours = [a for a in root.findall("action") if (a.findtext("unique-id") or "").startswith(FM_MARK)]
    for a in ours:
        root.remove(a)
    if install:
        pats = ";".join(sorted({"*" + e for e in ALL} | {"*" + e.upper() for e in ALL}))
        for verb, name in LINUX_VERBS:
            a = ET.SubElement(root, "action")
            for tag, text in (("icon", "multizip"), ("name", "MultiZip: " + name), ("unique-id", FM_MARK + verb),
                              ("command", f"{cmd} {verb} %F"), ("description", name),
                              ("patterns", "*" if verb == "a" else pats)):
                ET.SubElement(a, tag).text = text
            for tag in (("directories",) if verb == "a" else ()) + (
                    "audio-files", "image-files", "other-files", "text-files", "video-files"):
                ET.SubElement(a, tag)
    elif not ours:
        return ""
    if hasattr(ET, "indent"):
        ET.indent(root, "\t")
    _write_file(uca, '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n",
                os.stat(uca).st_mode & 0o777 if os.path.exists(uca) else 0o644)
    return ("Right-click menu in Thunar (Xfce) - restart Thunar to see it:  thunar -q" if install
            else "Removed the right-click menu from Thunar")


def linux_integration(install=True):
    """Put MultiZip in the applications menu and in the right-click menu of the file managers that are
    installed (Files / Nautilus, Caja, Dolphin, Thunar, Nemo), or take all of it out again.
    Returns what was done, one line each."""
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    conf = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    app = _app_command()
    cmd = " ".join(_desktop_quote(x) for x in app)
    archives, done = ";".join(LINUX_MIME) + ";", []

    desktop = os.path.join(data, "applications", "multizip.desktop")
    if install:
        _write_file(desktop, "[Desktop Entry]\nType=Application\nVersion=1.0\nName=MultiZip\n"
                    "GenericName=Archive Manager\nComment=Open, create and extract zip, 7z, rar, tar and other archives\n"
                    f"Exec={cmd} %f\nTryExec={app[0]}\nIcon=multizip\nTerminal=false\n"
                    "Categories=Utility;Archiving;Compression;\nKeywords=zip;7z;rar;tar;archive;extract;compress;unzip;\n"
                    f"MimeType={archives}\nStartupWMClass=Multizip\nStartupNotify=true\n", 0o644)
        done.append('Applications menu entry, and MultiZip in "Open with" for archives')
    elif _remove_file(desktop):
        done.append("Removed the applications menu entry")

    # GNOME Files (Nautilus) and Caja: scripts, shown as right-click > Scripts > MultiZip
    for prog, label, folder in (("nautilus", "Files (GNOME)", os.path.join(data, "nautilus", "scripts", "MultiZip")),
                                ("caja", "Caja (MATE)", os.path.join(conf, "caja", "scripts", "MultiZip"))):
        if install and shutil.which(prog):
            sh = " ".join(_shell_quote(x) for x in app)
            for verb, name in LINUX_VERBS:
                _write_file(os.path.join(folder, name), SCRIPT_MENU.replace("@NAME@", name).replace("@CMD@", sh)
                            .replace("@VERB@", verb), 0o755)
            done.append(f"Right-click menu in {label}:  Scripts > MultiZip")
        elif not install:
            gone = [n for _v, n in LINUX_VERBS if _remove_file(os.path.join(folder, n))]
            try:
                os.rmdir(folder)
            except OSError:
                pass
            if gone:
                done.append(f"Removed the right-click menu from {label}")

    # KDE Dolphin: service menus (they must be executable)
    kio = os.path.join(data, "kio", "servicemenus")
    extract = [v for v in LINUX_VERBS if v[0] != "a"]
    for fname, head, verbs in (
            (FM_MARK + "extract.desktop", f"MimeType={archives}\nX-KDE-Submenu=MultiZip\n", extract),
            (FM_MARK + "add.desktop", "MimeType=all/all;\n", [("a", "MultiZip: Add to archive...")])):
        path = os.path.join(kio, fname)
        if install and shutil.which("dolphin"):
            _write_file(path, "[Desktop Entry]\nType=Service\nX-KDE-ServiceTypes=KonqPopupMenu/Plugin\n" + head +
                        "Icon=multizip\nActions=" + "".join(f"multizip{v};" for v, _n in verbs) + "\n" +
                        "".join(f"\n[Desktop Action multizip{v}]\nName={n}\nIcon=multizip\nExec={cmd} {v} %F\n"
                                for v, n in verbs), 0o755)
        elif not install and _remove_file(path) and fname.endswith("extract.desktop"):
            done.append("Removed the right-click menu from Dolphin")
    if install and shutil.which("dolphin"):
        done.append("Right-click menu in Dolphin (KDE):  MultiZip")

    if (install and shutil.which("thunar")) or not install:
        msg = _thunar_actions(os.path.join(conf, "Thunar", "uca.xml"), cmd, install)
        if msg:
            done.append(msg)

    # Cinnamon's Nemo: one action file each
    nemo, gone = os.path.join(data, "nemo", "actions"), False
    for verb, name in LINUX_VERBS:
        path = os.path.join(nemo, f"{FM_MARK}{verb}.nemo_action")
        if install and shutil.which("nemo"):
            _write_file(path, f"[Nemo Action]\nName=MultiZip: {name}\nComment={name}\nExec={cmd} {verb} %F\n"
                        "Icon-Name=multizip\nSelection=notnone\n" +
                        ("Extensions=any;\n" if verb == "a" else f"Mimetypes={archives}\n"), 0o644)
        elif not install:
            gone = _remove_file(path) or gone
    if install and shutil.which("nemo"):
        done.append("Right-click menu in Nemo (Cinnamon)")
    elif gone:
        done.append("Removed the right-click menu from Nemo")

    if install and not any(shutil.which(p) for p in ("nautilus", "caja", "dolphin", "thunar", "nemo")):
        done.append("No supported file manager was found, so no right-click menu was added "
                    "(Files, Dolphin, Thunar, Nemo and Caja are supported)")
    return done


def main():
    a = sys.argv[1:]
    if a[:1] == ["--linux-integration"]:          # used by install-linux.sh: install / remove
        for line in linux_integration(a[1:2] != ["remove"]):
            print("  " + line)
        return
    if a[:1] == ["--get-engine"]:                 # used by build.bat to bundle 7-Zip into the .exe
        try:
            download_engine(lambda n=1, label=None: print(label or ""), lambda: False,
                            dest=os.path.abspath(a[1]) if len(a) > 1 else None)
            print("7-Zip engine ready.")
        except Exception as e:
            print("Could not get the 7-Zip engine:", e)
            sys.exit(1)
        return
    app = Main()
    if a and a[0] in {"x", "xh", "xd", "a", "t"} and len(a) > 1:
        c, files = a[0], a[1:]
        app.withdraw()

        def go():
            try:                                      # the window is hidden: always close, or the process lingers
                arcs = [f for f in files if kind(f)]
                if c != "a" and not arcs:
                    messagebox.showerror(APP, ENGINE_HELP if any(f.lower().endswith(ENGINE_EXTS) for f in files)
                                         else "MultiZip cannot open these files as archives.", parent=app)
                elif c == "x":
                    extract_many(app, arcs, None, True, None)
                elif c == "xh":
                    extract_many(app, arcs, None, False, None)
                elif c == "xd":
                    app.extract_dialog(arcs)
                elif c == "t":
                    extract_many(app, arcs, None, False, None, test=True)
                else:
                    add_to_archive(app, files, app.S)
            except Exception as e:
                messagebox.showerror(APP, str(e) or type(e).__name__, parent=app)
            finally:
                app.destroy()

        app.after(100, go)
    elif a[:1] == ["--usb-tool"]:                   # started again with administrator rights
        app.after(300, app.on_usb)
    elif a:
        app.after(100, lambda: app.goto(a[0]))
    app.mainloop()


if __name__ == "__main__":
    main()
