"""Tests for MultiZip's Linux integration (menu entry + file-manager right-click menus), on a fake home.
Usage: linuxint.py <src>"""
import configparser, contextlib, io, os, re, shlex, shutil, sys, tempfile
import xml.etree.ElementTree as ET

src = sys.argv[1]
sys.path.insert(0, src)
import multizip as m

work = tempfile.mkdtemp(prefix="mzlin_")


def check(name, ok, detail=""):
    print(("[ok]   " if ok else "[FAIL] ") + name + (f"  ({detail})" if detail else ""))


def desktop_unquote(field):
    """Split an Exec value the way the desktop-entry spec (and Dolphin/Thunar/Nemo) read it."""
    out, i, cur, quoted, have = [], 0, "", False, False
    while i < len(field):
        c = field[i]
        if quoted:
            if c == "\\" and i + 1 < len(field) and field[i + 1] in '"`$\\':
                cur, i = cur + field[i + 1], i + 2
                continue
            if c == '"':
                quoted = False
            else:
                cur += c
        elif c == '"':
            quoted, have = True, True
        elif c == " ":
            if cur or have:
                out.append(cur)
            cur, have = "", False
        else:
            cur += c
        i += 1
    if cur or have:
        out.append(cur)
    return [a.replace("%%", "%") if not re.fullmatch(r"%[fFuU]", a) else a for a in out]


def ini(path):
    p = configparser.ConfigParser(interpolation=None, strict=True)
    p.optionxform = str
    p.read(path, encoding="utf-8")
    return p


try:
    home = os.path.join(work, "home")
    data, conf = os.path.join(home, ".local", "share"), os.path.join(home, ".config")
    os.environ.update(XDG_DATA_HOME=data, XDG_CONFIG_HOME=conf, HOME=home, USERPROFILE=home)
    exe = os.path.join(home, "My Apps", "it's $HOME 100%", "MultiZip")      # a deliberately awkward path
    sys.frozen, sys_exe = True, sys.executable
    sys.executable = exe
    real_which = shutil.which
    shutil.which = lambda p: "/usr/bin/" + p if p in ("nautilus", "caja", "dolphin", "thunar", "nemo") else None
    modes, pending = {}, {}                      # file -> permissions it was given (via its temporary name)
    real_chmod, real_replace = os.chmod, os.replace
    os.chmod = lambda p, mode: (pending.__setitem__(p, mode), real_chmod(p, mode))

    def track_replace(a, b):
        if a in pending:
            modes[b] = pending.pop(a)
        real_replace(a, b)
    os.replace = track_replace

    # Thunar already has one action of the user's
    uca = os.path.join(conf, "Thunar", "uca.xml")
    os.makedirs(os.path.dirname(uca))
    open(uca, "w", encoding="utf-8").write('<?xml version="1.0" encoding="UTF-8"?>\n<actions>\n<action>\n'
                                           '\t<icon>utilities-terminal</icon>\n\t<name>Open Terminal Here</name>\n'
                                           '\t<unique-id>1234-1</unique-id>\n\t<command>exo-open --launch '
                                           'TerminalEmulator</command>\n\t<patterns>*</patterns>\n'
                                           '\t<directories/>\n</action>\n</actions>\n')

    done = m.linux_integration(True)
    m.linux_integration(True)                    # running the installer twice must not duplicate anything
    print("   install said: " + " | ".join(done))
    check("install reports every file manager", sum("Right-click menu in" in d for d in done) == 5 and
          any("Applications menu" in d for d in done))

    # applications menu entry
    d = ini(os.path.join(data, "applications", "multizip.desktop"))["Desktop Entry"]
    check("menu entry: runs the program with one file", desktop_unquote(d["Exec"]) == [exe, "%f"], d["Exec"])
    check("menu entry: TryExec / icon / class / categories", d["TryExec"] == exe and d["Icon"] == "multizip" and
          d["StartupWMClass"] == "Multizip" and d["Categories"] == "Utility;Archiving;Compression;")
    check("menu entry: 25 archive types", len(d["MimeType"].rstrip(";").split(";")) == 25 == len(set(m.LINUX_MIME)))

    verbs = {v: n for v, n in m.LINUX_VERBS}
    check("same five actions as the Windows right-click menu", set(verbs) == {"xh", "x", "xd", "t", "a"} and
          all(f"'{v}'" in open(os.path.join(src, "installer.iss"), encoding="utf-8").read() for v in verbs))

    # GNOME Files and Caja scripts
    for label, folder in (("Files", os.path.join(data, "nautilus", "scripts", "MultiZip")),
                          ("Caja", os.path.join(conf, "caja", "scripts", "MultiZip"))):
        names = sorted(os.listdir(folder))
        win = lambda n: n.rstrip(".") if os.name == "nt" else n         # Windows drops trailing dots; Linux keeps them
        ok = names == sorted(win(n) for n in verbs.values())
        for v, n in verbs.items():
            text = open(os.path.join(folder, win(n)), encoding="utf-8").read()
            lines = [l.strip() for l in text.splitlines() if l.strip().startswith("exec ")]
            argv = [shlex.split(l)[1:] for l in lines]
            ok = ok and text.startswith("#!/bin/sh\n") and argv == [[exe, v, "$paths"], [exe, v, "$@"]] \
                and modes.get(os.path.join(folder, n)) == 0o755 and "\r" not in text
        check(f"{label}: 5 executable scripts that run MultiZip with the right action", ok, str(names))

    # Dolphin service menus
    kio = os.path.join(data, "kio", "servicemenus")
    ex, add = ini(os.path.join(kio, "multizip-extract.desktop")), ini(os.path.join(kio, "multizip-add.desktop"))
    acts = ex["Desktop Entry"]["Actions"].rstrip(";").split(";")
    ok = (ex["Desktop Entry"]["Type"] == "Service" and ex["Desktop Entry"]["X-KDE-Submenu"] == "MultiZip"
          and len(ex["Desktop Entry"]["MimeType"].split(";")) == 26 and acts == [f"multizip{v}" for v in verbs if v != "a"]
          and all(desktop_unquote(ex[f"Desktop Action {a}"]["Exec"]) == [exe, a[8:], "%F"] for a in acts))
    check("Dolphin: archive submenu with 4 actions", ok)
    check("Dolphin: Add to archive on any file or folder",
          add["Desktop Entry"]["MimeType"] == "all/all;" and
          desktop_unquote(add["Desktop Action multizipa"]["Exec"]) == [exe, "a", "%F"])
    check("Dolphin: service menus are executable (Dolphin ignores them otherwise)",
          all(modes.get(os.path.join(kio, f)) == 0o755 for f in ("multizip-extract.desktop", "multizip-add.desktop")))

    # Thunar
    root = ET.parse(uca).getroot()
    ids = [a.findtext("unique-id") for a in root.findall("action")]
    check("Thunar: user's own action kept, ours added once", ids == ["1234-1"] + [f"multizip-{v}" for v in verbs], str(ids))
    acts = {a.findtext("unique-id"): a for a in root.findall("action")}
    xh, aa = acts["multizip-xh"], acts["multizip-a"]
    check("Thunar: commands", desktop_unquote(xh.findtext("command")) == [exe, "xh", "%F"] and
          desktop_unquote(aa.findtext("command")) == [exe, "a", "%F"])
    pats = xh.findtext("patterns").split(";")
    check("Thunar: archive patterns (both cases), Add works on folders",
          "*.zip" in pats and "*.ZIP" in pats and "*.tar.gz" in pats and aa.find("directories") is not None
          and xh.find("directories") is None and xh.find("other-files") is not None)

    # Nemo
    nemo = os.path.join(data, "nemo", "actions")
    ok = True
    for v, n in verbs.items():
        a = ini(os.path.join(nemo, f"multizip-{v}.nemo_action"))["Nemo Action"]
        ok = ok and desktop_unquote(a["Exec"]) == [exe, v, "%F"] and a["Selection"] == "notnone" and \
            (a.get("Extensions") == "any;" if v == "a" else len(a["Mimetypes"].split(";")) == 26)
    check("Nemo: 5 actions", ok)

    # uninstall
    gone = m.linux_integration(False)
    print("   remove said: " + " | ".join(gone))
    left = sorted(os.path.relpath(os.path.join(r, f), home) for r, _d, fs in os.walk(home) for f in fs)
    check("remove: only the user's Thunar file is left", left == [os.path.join(".config", "Thunar", "uca.xml")], str(left))
    ids = [a.findtext("unique-id") for a in ET.parse(uca).getroot().findall("action")]
    check("remove: Thunar keeps the user's own action", ids == ["1234-1"], str(ids))
    check("remove: reports each place", len(gone) == 6, str(len(gone)))
    check("remove again: nothing to do, no errors", m.linux_integration(False) == [])

    # no file manager at all, and an unreadable Thunar file
    shutil.which = lambda p: None
    done = m.linux_integration(True)
    check("no file manager: menu entry only, and says why", len(done) == 2 and "No supported file manager" in done[1])
    m.linux_integration(False)
    open(uca, "w").write("<actions><action>broken")
    shutil.which = lambda p: "/usr/bin/thunar" if p == "thunar" else None
    done = m.linux_integration(True)
    check("Thunar file damaged: left untouched", open(uca).read() == "<actions><action>broken" and
          any("left alone" in d for d in done), str(done))

    # run from source, and through the command line
    del sys.frozen
    sys.executable = "/usr/bin/python3"
    check("from source: menus run python3 multizip.py", m._app_command() == ["/usr/bin/python3", os.path.abspath(m.__file__)])
    shutil.which = lambda p: None
    out = io.StringIO()
    sys.argv = ["multizip", "--linux-integration", "install"]
    real_main = m.Main
    m.Main = lambda: (_ for _ in ()).throw(AssertionError("opened a window"))
    with contextlib.redirect_stdout(out):
        m.main()
    m.Main = real_main
    check("command line: --linux-integration install prints, opens no window", "Applications menu" in out.getvalue())
finally:
    os.chmod, os.replace = real_chmod, real_replace
    shutil.which = real_which
    shutil.rmtree(work, ignore_errors=True)
