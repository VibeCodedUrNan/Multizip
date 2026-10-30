"""7-Zip engine extraction: passwords, selected files, non-ASCII names, odd archive names. Usage: engine.py <src>"""
import os, shutil, sys, tempfile

sys.path.insert(0, sys.argv[1])
import multizip as m

noop = lambda n=1, label=None: None
never = lambda: False


def check(name, ok, detail=""):
    print(("[ok]   " if ok else "[FAIL] ") + name + (f"  ({detail})" if detail else ""))


def files(d):
    return sorted(os.path.relpath(os.path.join(r, f), d).replace("\\", "/") for r, _x, fs in os.walk(d) for f in fs)


if not m.engine_path():
    print("[FAIL] 7-Zip engine not found - install 7-Zip to run these checks")
    sys.exit()
work = tempfile.mkdtemp(prefix="mzeng_")
try:
    src = os.path.join(work, "src")
    os.makedirs(os.path.join(src, "sub"))
    for rel, text in (("a.txt", "plain"), ("sub/b.txt", "deep"), ("Ünïcödé €.txt", "utf")):
        open(os.path.join(src, rel), "w", encoding="utf-8").write(text)
    for fmt in ("zip", "7z"):
        arc = os.path.join(work, "secret." + fmt)
        m.engine_create(arc, [os.path.join(src, n) for n in ("a.txt", "sub", "Ünïcödé €.txt")], fmt, 6, "pw 123", noop, never)
        d = os.path.join(work, "all_" + fmt)
        m.engine_extract(arc, d, "pw 123", None, noop, never)
        check(f"encrypted {fmt}: everything extracts with the password",
              files(d) == ["a.txt", "sub/b.txt", "Ünïcödé €.txt"] and open(os.path.join(d, "sub", "b.txt")).read() == "deep",
              str(files(d)))
        d = os.path.join(work, "one_" + fmt)
        m.engine_extract(arc, d, "pw 123", ["sub/b.txt", "Ünïcödé €.txt"], noop, never)
        check(f"encrypted {fmt}: only the selected files", files(d) == ["sub/b.txt", "Ünïcödé €.txt"], str(files(d)))
        try:
            m.engine_extract(arc, os.path.join(work, "bad_" + fmt), "wrong", None, noop, never)
            check(f"encrypted {fmt}: wrong password is reported", False)
        except RuntimeError as e:
            check(f"encrypted {fmt}: wrong password is reported", "password" in str(e).lower(), str(e)[:60])
    # an archive whose file name starts with "-", passed as a relative path
    old = os.getcwd()
    os.chdir(work)
    try:
        shutil.copy(os.path.join(work, "secret.zip"), "-oEVIL.zip")
        m.FORCED[os.path.abspath("-oEVIL.zip")] = "engine"
        m.engine_extract("-oEVIL.zip", os.path.join(work, "dash"), "pw 123", None, noop, never)
        m.engine_test("-oEVIL.zip", "pw 123", noop, never)
        check("archive named -oEVIL.zip is read as a file, not an option",
              files(os.path.join(work, "dash")) == ["a.txt", "sub/b.txt", "Ünïcödé €.txt"] and not os.path.exists("EVIL"))
    finally:
        os.chdir(old)
finally:
    shutil.rmtree(work, ignore_errors=True)
