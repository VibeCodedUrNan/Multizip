import inspect, os, shutil, sys, tempfile, traceback, zipfile

sys.path.insert(0, sys.argv[1])
import multizip as m

print("python", sys.version.split()[0], "| ZSTD", m.ZSTD, "| py7zr", getattr(m.py7zr, "__version__", None))
print("rmtree params:", list(inspect.signature(shutil.rmtree).parameters))
print("engine:", m.engine_path(), m._ENGINE["ver"])

noop = lambda n=1, label=None: None
never = lambda: False
work = tempfile.mkdtemp(prefix="mzprobe_")
src = os.path.join(work, "src")
os.makedirs(os.path.join(src, "sub"))
for rel, data in (("a.txt", b"hello"), ("sub/b.txt", b"world" * 1000)):
    with open(os.path.join(src, rel), "wb") as f:
        f.write(data)
extra = os.path.join(work, "extra.txt")
with open(extra, "wb") as f:
    f.write(b"extra")


def step(name, fn):
    try:
        r = fn()
        print(f"[ok]   {name}: {r if r is not None else ''}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=2, file=sys.stdout)


for fmt in ("7z", "zip", "tar.gz", "tar.zst"):
    out = os.path.join(work, "t." + fmt)
    step(f"create {fmt}", lambda: m.create_archive(out, [src], fmt, 6, None, noop, never))
    step(f"list {fmt}", lambda: sorted(e[0] for e in m.list_archive(out)))
    dest = os.path.join(work, "x_" + fmt.replace(".", "_"))
    step(f"extract one member {fmt}", lambda: (m.extract(out, dest, None, ["src/sub/b.txt"], noop, never),
                                               sorted(os.path.relpath(os.path.join(r, f), dest)
                                                      for r, _, fs in os.walk(dest) for f in fs))[1])

if m.py7zr:
    step("7z append (add_into)", lambda: (m.add_into(os.path.join(work, "t.7z"), [extra], "", None, noop, never),
                                          sorted(e[0] for e in m.list_archive(os.path.join(work, "t.7z"))))[1])
else:                                               # no usable py7zr: a clear message, and the 7z left untouched
    def refused():
        before = open(os.path.join(work, "t.7z"), "rb").read()
        try:
            m.add_into(os.path.join(work, "t.7z"), [extra], "", None, noop, never)
        except RuntimeError as e:
            assert e.args[0] == m.NO_7Z_ADD and open(os.path.join(work, "t.7z"), "rb").read() == before
            return "refused with a message"
        raise AssertionError("added without py7zr")
    step("7z append without py7zr", refused)

# zip: adding a file that is already in the archive
z = os.path.join(work, "t.zip")
step("zip add same file twice", lambda: (m.add_into(z, [extra], "", None, noop, never),
                                         m.add_into(z, [extra], "", None, noop, never),
                                         [n for n in zipfile.ZipFile(z).namelist() if n == "extra.txt"])[2])
step("zip_delete", lambda: (m.zip_delete(z, ["src/sub"]), sorted(zipfile.ZipFile(z).namelist()))[1])

if m.engine_path():
    e = os.path.join(work, "e.zip")
    shutil.copy(os.path.join(work, "t.zip"), e)
    dest = os.path.join(work, "x_engine")
    step("engine_extract one member", lambda: (m.engine_extract(e, dest, None, ["src/a.txt"], noop, never),
                                               sorted(os.path.relpath(os.path.join(r, f), dest)
                                                      for r, _, fs in os.walk(dest) for f in fs))[1])

shutil.rmtree(work, ignore_errors=True)
