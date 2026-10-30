"""Regression probes for the fixes. Usage: regress.py <src dir> <case>"""
import os, shutil, subprocess, sys, tempfile, zipfile

sys.path.insert(0, sys.argv[1])
import multizip as m

case = sys.argv[2]
shown = []
for fn in ("showerror", "showinfo", "showwarning"):
    setattr(m.messagebox, fn, lambda title, msg, _fn=fn, **k: shown.append((_fn, msg)))
m.messagebox.askyesno = lambda *a, **k: True
noop = lambda n=1, label=None: None
never = lambda: False
work = tempfile.mkdtemp(prefix="mzreg_")


def check(name, ok, detail=""):
    print(("[ok]   " if ok else "[FAIL] ") + name + (f"  ({detail})" if detail else ""))


try:
    if case == "diskpart":
        real = m.subprocess.run
        m.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 1, "DiskPart has encountered an error: Access is denied.", "")
        try:
            m.run_diskpart("exit\r\n")
            check("diskpart failure raises", False)
        except RuntimeError as e:
            check("diskpart failure raises", "Access is denied" in str(e), str(e).splitlines()[0])
        m.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "DiskPart successfully cleaned the disk.", "")
        check("diskpart success returns output", "successfully" in m.run_diskpart("exit\r\n"))
        m.subprocess.run = real

    elif case.startswith("cli"):
        f = os.path.join(work, "notes.txt" if case != "cli_iso" else "disk.iso")
        open(f, "wb").close()
        if case == "cli_iso":
            m.engine_path = lambda refresh=False: None
        if case == "cli_crash":
            f = os.path.join(work, "a.zip")
            zipfile.ZipFile(f, "w").writestr("x.txt", "x")
            m.Main.extract_dialog = lambda self, a: 1 / 0
        sys.argv = ["multizip", "xd", f]
        import threading
        t = threading.Timer(20, lambda: os._exit(3))      # if main() never returns, the process is stuck
        t.start()
        m.main()
        t.cancel()
        check(f"{case}: main() returned (no hidden process)", True)
        check(f"{case}: user was told", bool(shown), shown[0][1].splitlines()[0] if shown else "nothing shown")

    elif case == "password":
        arc = os.path.join(work, "secret.zip")
        src = os.path.join(work, "s.txt")
        open(src, "w").write("secret")
        m.engine_create(arc, [src], "zip", 6, "pw123", noop, never)
        m.simpledialog.askstring = lambda *a, **k: None          # user cancels the password prompt
        root = m.tk.Tk()
        root.withdraw()
        ok = m.extract_many(root, [arc], None, False, None, test=True)
        check("test returns failure", ok is False)
        check("no 'No errors found.'", not any("No errors" in s for _f, s in shown), str(shown))
        check("password skip reported", any("needs a password" in s for _f, s in shown))
        root.destroy()

    elif case == "zipadd":
        src = os.path.join(work, "src")
        os.makedirs(src)
        open(os.path.join(src, "keep.txt"), "w").write("keep")
        z = os.path.join(work, "t.zip")
        m.create_archive(z, [src], "zip", 6, None, noop, never)
        extra = os.path.join(work, "extra.txt")
        for text in ("v1", "v2"):
            open(extra, "w").write(text)
            m.add_into(z, [extra], "", None, noop, never)
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
            check("re-added file stored once", names.count("extra.txt") == 1, str(names))
            check("re-added file has new content", zf.read("extra.txt") == b"v2")
        os.remove(os.path.join(src, "keep.txt"))
        open(os.path.join(src, "new.txt"), "w").write("new")
        m.add_into(z, [src], "", None, noop, never)              # re-add the folder with different contents
        with zipfile.ZipFile(z) as zf:
            names = sorted(zf.namelist())
        check("folder re-add keeps other files, no duplicates",
              names == ["extra.txt", "src/", "src/keep.txt", "src/new.txt"], str(names))
        enc = os.path.join(work, "enc.zip")
        m.engine_create(enc, [extra], "zip", 6, "pw", noop, never)
        try:
            m.add_into(enc, [os.path.join(src, "new.txt")], "", None, noop, never)
            check("encrypted zip refused", False)
        except RuntimeError as e:
            check("encrypted zip refused", "password-protected" in str(e))
        check("encrypted zip unchanged", zipfile.ZipFile(enc).namelist() == ["extra.txt"])

    elif case == "fmt":
        root = m.BaseTk()
        root.withdraw()
        d = m.AddDialog(root, [], dict(m.DEFAULTS))
        d.out.set(r"C:\x\name.tar.zst")
        d.fmt.set("zip")
        d.on_fmt()
        check("tar.zst -> zip name", d.out.get() == r"C:\x\name.zip", d.out.get())
        d.destroy()
        root.destroy()
finally:
    shutil.rmtree(work, ignore_errors=True)
