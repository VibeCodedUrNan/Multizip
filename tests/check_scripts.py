"""Checks for build.sh and install-linux.sh that need no Linux, the bundled-7zz fix, and the Linux window class.
Usage: check_scripts.py <repo folder>"""
import os, re, shutil, sys, tempfile

src = sys.argv[1]


def check(name, ok, detail=""):
    print(("[ok]   " if ok else "[FAIL] ") + name + (f"  ({detail})" if detail else ""))


for script in ("build.sh", "install-linux.sh"):
    raw = open(os.path.join(src, script), "rb").read()
    text = raw.decode("utf-8")
    lines = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    count = lambda pat: sum(len(re.findall(pat, l)) for l in lines)
    check(f"{script}: Unix line endings, bash shebang, ASCII",
          b"\r" not in raw and raw.startswith(b"#!/usr/bin/env bash\n") and all(b < 128 for b in raw))
    pairs = {"if/fi": (count(r";\s*then\b") - count(r"^\s*elif\b"), count(r"(^\s*|;\s*)fi\b")),
             "do/done": (count(r";\s*do\b"), count(r"^\s*done\b")),
             "case/esac": (count(r"^\s*case\b"), count(r"^\s*esac\b"))}
    check(f"{script}: blocks balance", all(a == b for a, b in pairs.values()), str(pairs))
    newer = [w for w in ("declare -A", "mapfile", "readarray", ",,}", "^^}", "&>>", "|&", "coproc", "[[ -v", "readlink -f")
             if w in text]
    check(f"{script}: works with the bash 3.2 macOS ships", not newer, str(newer))

bat = open(os.path.join(src, "build.bat"), encoding="utf-8").read()
sh = open(os.path.join(src, "build.sh"), encoding="utf-8").read()
for opt in ("--exclude-module PIL._avif", "--exclude-module PIL.AvifImagePlugin", "--exclude-module setuptools",
            "--exclude-module pkg_resources", "--exclude-module zstandard", "--exclude-module py7zr"):
    check(f"build.bat and build.sh both use {opt}", opt in bat and opt.split()[1] in sh)
check("both builds refuse py7zr older than 0.20.2", bat.count("(0, 20, 2)") == 1 and sh.count("(0, 20, 2)") == 1)

sys.path.insert(0, src)
import multizip as m

# a bundled 7zz that unpacked without its executable bit gets it back (simulated Linux)
d = tempfile.mkdtemp()
f = os.path.join(d, "7zz")
open(f, "wb").write(b"\x7fELF")
changed = []
old = os.name, getattr(sys, "_MEIPASS", None), os.access, os.chmod
try:
    os.name, sys._MEIPASS = "posix", d
    os.access = lambda p, mode: False
    os.chmod = lambda p, mode: changed.append((p, mode & 0o111))
    found = m._engine_candidates()
finally:
    os.name, os.access, os.chmod = old[0], old[2], old[3]
    if old[1] is None:
        del sys._MEIPASS
    else:
        sys._MEIPASS = old[1]
    shutil.rmtree(d, ignore_errors=True)
check("bundled 7zz found and made executable", f in found and changed and changed[0] == (f, 0o111))

# Linux: the window class must match the menu entry's StartupWMClass, and the icon must load
w = tempfile.mkdtemp()
env = {k: os.environ.get(k) for k in ("XDG_DATA_HOME", "XDG_CONFIG_HOME")}
os.environ.update(XDG_DATA_HOME=w, XDG_CONFIG_HOME=w)
which = shutil.which
shutil.which = lambda p: None
try:
    m.linux_integration(True)
    entry = re.search(r"StartupWMClass=(\S+)", open(os.path.join(w, "applications", "multizip.desktop")).read()).group(1)
finally:
    shutil.which = which
    for k, v in env.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    shutil.rmtree(w, ignore_errors=True)
real = os.name
os.name = "posix"
try:
    app = m.Main()
    app.withdraw()
    cls, icon = app.winfo_class(), getattr(app, "_icon", None)
    icon_ok = icon is not None and icon.width() == 256
    app.destroy()
finally:
    os.name = real
check("Linux window class matches the menu entry", cls == entry, f"window {cls!r}, entry {entry!r}")
check("Linux window icon loads", icon_ok)
app = m.Main()
app.withdraw()
check("Windows window class and icon unchanged", app.winfo_class() == "Tk" and not hasattr(app, "_icon"))
app.destroy()
