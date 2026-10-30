"""Simulated Linux / macOS checks. Usage: xplat.py <src dir>"""
import contextlib, json, os, shutil, sys, tempfile, types

sys.path.insert(0, sys.argv[1])
import multizip as m

work = tempfile.mkdtemp(prefix="mzx_")


def check(name, ok, detail=""):
    print(("[ok]   " if ok else "[FAIL] ") + name + (f"  ({detail})" if detail else ""))


@contextlib.contextmanager
def platform(osname, plat):
    old = os.name, sys.platform
    os.name, sys.platform = osname, plat
    try:
        yield
    finally:
        os.name, sys.platform = old


@contextlib.contextmanager
def patched(obj, **kw):
    old = {k: getattr(obj, k) for k in kw}
    for k, v in kw.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


try:
    # 1. drive list: another user's /media folder is unreadable
    J = os.path.join
    tree = {"/media": ["me", "other"], J("/media", "me"): ["USB"], "/mnt": [], "/Volumes": None, "/run/media": None}

    def fake_ls(d):
        if d == J("/media", "other"):
            raise PermissionError(13, "Permission denied", d)
        if tree.get(d) is None:
            raise FileNotFoundError(2, "No such file", d)
        return tree[d]
    with platform("posix", "linux"), patched(os, listdir=fake_ls), \
            patched(os.path, isdir=lambda p: True, ismount=lambda p: p == J("/media", "me", "USB")), \
            patched(shutil, disk_usage=lambda p: types.SimpleNamespace(free=1 << 30, total=4 << 30)):
        try:
            drives = [p for _t, p in m.list_drives()]
            check("drive list survives an unreadable /media/<user>", J("/media", "me", "USB") in drives and "/" in drives,
                  str(drives))
        except Exception as e:
            check("drive list survives an unreadable /media/<user>", False, f"{type(e).__name__}: {e}")

    # 2. opening files and folders
    calls = []
    fake_popen = lambda argv, **k: calls.append(list(argv))
    d = J(work, "folder")
    os.makedirs(d)
    f = J(d, "file.txt")
    open(f, "w").close()
    with patched(m.subprocess, Popen=fake_popen):
        for osname, plat in (("posix", "linux"), ("posix", "darwin")):
            calls.clear()
            with platform(osname, plat):
                m.open_path(f)
                m.open_folder(d)
                m.reveal_file(f)
            opener = "open" if plat == "darwin" else "xdg-open"
            want = [[opener, f], [opener, d], ["open", "-R", f] if plat == "darwin" else ["xdg-open", d]]
            check(f"{plat}: open file / open folder / show in folder", calls == want, str(calls))
        calls.clear()
        started = []
        with patched(os, startfile=lambda p: started.append(p)):
            m.open_path(f)
            m.open_folder(d)
            m.reveal_file(f)
        check("Windows: unchanged (startfile, explorer /select)", started == [f, d] and calls == [["explorer", "/select," + f]],
              f"{started} {calls}")
        with platform("posix", "linux"), patched(m.subprocess, Popen=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("xdg-open"))):
            try:
                m.open_path(f)
                check("missing opener raises OSError for the error message", False)
            except OSError:
                check("missing opener raises OSError for the error message", True)

    # 3. disk mounts and Scan all drives
    mounts = ("/dev/nvme0n1p2 / ext4 rw 0 0\nproc /proc proc rw 0 0\nsysfs /sys sysfs rw 0 0\ntmpfs /run tmpfs rw 0 0\n"
              "/dev/nvme0n1p3 /home ext4 rw 0 0\n/dev/loop3 /snap/core/1 squashfs ro 0 0\n"
              "//nas/share /mnt/nas cifs rw 0 0\nnas:/data /mnt/nfs nfs4 rw 0 0\n/dev/sdb1 /media/me/My\\040Stick vfat rw 0 0\n"
              "/dev/nvme0n1p3 /home ext4 rw 0 0\n")
    got = m.disk_mounts(mounts)
    check("Linux mounts: real disks only", got == ["/", "/home", "/media/me/My Stick"], str(got))

    real_open = open

    def fake_open(p, *a, **k):
        if p == "/proc/self/mounts":
            import io
            return io.StringIO(mounts)
        return real_open(p, *a, **k)
    with platform("posix", "linux"), patched(os.path, isdir=lambda p: True):
        m.open = fake_open
        try:
            roots = m.local_drive_roots()
        finally:
            del m.open
    check("Linux Scan all drives = each real disk", roots == ["/", "/home", "/media/me/My Stick"], str(roots))
    with platform("posix", "darwin"), patched(os.path, isdir=lambda p: p == "/System/Volumes/Data"), \
            patched(m, list_drives=lambda: [("This PC", ""), ("/", "/"), ("/Volumes/Backup", "/Volumes/Backup")]):
        roots = m.local_drive_roots()
    check("macOS Scan all drives = Data volume + external disks", roots == ["/System/Volumes/Data", "/Volumes/Backup"],
          str(roots))

    # 4. scanning stays on one disk (fake / with /proc on another device)
    class E:
        def __init__(self, path, isdir, dev, size=0):
            self.path, self.name, self._d, self._st = path, os.path.basename(path), isdir, types.SimpleNamespace(
                st_dev=dev, st_size=size, st_mtime=0, st_file_attributes=0)

        def is_symlink(self): return False
        def is_dir(self, follow_symlinks=True): return self._d
        def is_junction(self): return False
        def stat(self, follow_symlinks=True): return self._st

    fs = {"/": [E("/proc", True, 2), E("/home", True, 1), E("/big.iso", False, 1, 700)],
          "/home": [E("/home/a.txt", False, 1, 5)],
          "/proc": [E("/proc/kcore", False, 2, 1 << 47)]}

    class Scan:
        def __init__(self, p): self.items = fs.get(p.replace("\\", "/"), [])
        def __enter__(self): return self
        def __iter__(self): return iter(self.items)
        def __exit__(self, *a): return False
    with platform("posix", "linux"), patched(os, scandir=Scan, stat=lambda p: types.SimpleNamespace(st_dev=1)), \
            patched(os.path, abspath=lambda p: p):
        r = m.scan_usage("/", lambda: False, lambda *a: None)
    check("Linux scan skips /proc (other device)", [d.name for d in r["root"].dirs] == ["home"] and r["stats"]["bytes"] == 705,
          f"dirs={[d.name for d in r['root'].dirs]} bytes={r['stats']['bytes']}")
    real = J(work, "scanme")
    os.makedirs(J(real, "sub"))
    open(J(real, "sub", "x.bin"), "wb").write(b"x" * 1000)
    r = m.scan_usage(real, lambda: False, lambda *a: None)
    check("Windows scan unchanged", r["stats"]["bytes"] == 1000 and [d.name for d in r["root"].dirs] == ["sub"])

    # 5. This PC on Linux lists / and mounted disks
    root = m.Main()
    root.withdraw()
    with platform("posix", "linux"), patched(m, list_drives=lambda: [("This PC", ""), ("/  System disk", "/"),
                                                                     ("/media/me/USB", "/media/me/USB")]):
        root.cwd, root.arc = "", None
        rows = [r["path"] for r in root.rows()]
    check("Linux This PC lists / and mounted disks", rows == ["/", "/media/me/USB"], str(rows))
    root.cwd = ""
    win_rows = [r["path"] for r in root.rows()]
    check("Windows This PC unchanged (drive letters)", win_rows and all(len(p) == 3 and p.endswith(":\\") for p in win_rows),
          str(win_rows))

    # 6. right-click and wheel
    class W:
        def __init__(self, ws):
            self.ws, self.binds = ws, {}
            self.tk = types.SimpleNamespace(call=lambda *a: self.ws)
        def bind(self, ev, fn): self.binds[ev] = fn
    for ws, want_right, want_wheel in (("aqua", {"<Button-3>", "<Button-2>"}, {"<MouseWheel>"}),
                                       ("x11", {"<Button-3>"}, {"<MouseWheel>", "<Button-4>", "<Button-5>"}),
                                       ("win32", {"<Button-3>"}, {"<MouseWheel>"})):
        w1, w2 = W(ws), W(ws)
        m.bind_right_click(w1, print)
        m.bind_wheel(w2, print)
        check(f"{ws}: right-click and wheel bindings", set(w1.binds) == want_right and set(w2.binds) == want_wheel,
              f"{sorted(w1.binds)} {sorted(w2.binds)}")
    pairs = {120: -1, -120: 1, 240: -2, 3: -1, -1: 1, 0: 0}
    got = {d: m.wheel_lines(d) for d in pairs}
    check("wheel: Windows notches unchanged, small macOS deltas scroll", got == pairs, str(got))
    root.destroy()

    # 7. settings folder (and moving an earlier ~/MultiZip)
    home, cfg = J(work, "home"), J(work, "cfg")
    old = J(home, "MultiZip")
    os.makedirs(old)
    json.dump({"theme": "Dark", "bg_image": J(old, "background.png")}, open(J(old, "settings.json"), "w"))
    open(J(old, "background.png"), "wb").write(b"png")
    env = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME", "XDG_CONFIG_HOME")}
    os.environ.update(USERPROFILE=home, HOME=home, XDG_CONFIG_HOME=cfg)
    try:
        with platform("posix", "linux"):
            p = m.settings_path()
            s = json.load(open(p))
        check("Linux settings in $XDG_CONFIG_HOME/MultiZip", p == J(cfg, "MultiZip", "settings.json"), p)
        check("earlier ~/MultiZip moved, background still found",
              not os.path.exists(old) and s["theme"] == "Dark" and os.path.isfile(s["bg_image"]), s.get("bg_image"))
        del os.environ["XDG_CONFIG_HOME"]
        with platform("posix", "darwin"):
            p = m.settings_path()
        check("macOS settings in ~/Library/Application Support/MultiZip",
              os.path.normpath(p) == os.path.normpath(J(home, "Library", "Application Support", "MultiZip", "settings.json")), p)
    finally:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("Windows settings unchanged (%APPDATA%)",
          m.settings_path() == J(os.environ["APPDATA"], "MultiZip", "settings.json"), m.settings_path())
finally:
    shutil.rmtree(work, ignore_errors=True)
