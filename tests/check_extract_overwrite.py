"""Regression probes for the third-pass fixes. Usage: fix3.py <src dir>"""
import gzip, os, shutil, sys, tempfile, zipfile

sys.path.insert(0, sys.argv[1])
import multizip as m

noop = lambda n=1, label=None: None
never = lambda: False
work = tempfile.mkdtemp(prefix="mzfix3_")
shown = []
for fn in ("showerror", "showinfo"):
    setattr(m.messagebox, fn, lambda title, msg, _fn=fn, **k: shown.append((_fn, msg)))


def check(name, ok, detail=""):
    print(("[ok]   " if ok else "[FAIL] ") + name + (f"  ({detail})" if detail else ""))


def read(p):
    return open(p, encoding="utf-8").read() if os.path.exists(p) else None


try:
    src = os.path.join(work, "src")
    os.makedirs(src)
    open(os.path.join(src, "notes.txt"), "w").write("archive notes")
    open(os.path.join(src, "new.txt"), "w").write("archive new")
    arcs = {}
    for fmt in ("zip", "7z", "tar.gz"):
        arcs[fmt] = os.path.join(work, "a." + fmt)
        m.create_archive(arcs[fmt], [os.path.join(src, "notes.txt"), os.path.join(src, "new.txt")], fmt, 6, None, noop, never)
    arcs["engine"] = os.path.join(work, "e.zip")
    shutil.copy(arcs["zip"], arcs["engine"])
    m.FORCED[os.path.abspath(arcs["engine"])] = "engine"
    arcs["gz"] = os.path.join(work, "notes.txt.gz")
    with gzip.open(arcs["gz"], "wb") as f:
        f.write(b"archive notes")

    root = m.tk.Tk()
    root.withdraw()
    for label, answer in (("No = keep", False), ("Yes = replace", True), ("Cancel = stop", None)):
        for fmt, a in arcs.items():
            dest = os.path.join(work, f"out_{fmt}_{answer}")
            os.makedirs(dest)
            open(os.path.join(dest, "notes.txt"), "w").write("MY NOTES")
            asked = []
            m.messagebox.askyesnocancel = lambda *x, _a=answer, **k: (asked.append(1), _a)[1]
            m.extract_many(root, [a], dest, False, None)
            notes, new = read(os.path.join(dest, "notes.txt")), read(os.path.join(dest, "new.txt"))
            want_notes = "MY NOTES" if answer is not True else "archive notes"
            want_new = None if (answer is None or fmt == "gz") else "archive new"
            check(f"{label:13} {fmt:6}", asked and notes == want_notes and new == want_new,
                  f"asked={bool(asked)} notes={notes!r} new={new!r}")
    dest = os.path.join(work, "fresh")
    asked = []
    m.messagebox.askyesnocancel = lambda *x, **k: asked.append(1)
    m.extract_many(root, [arcs["zip"]], dest, False, None)
    check("no question when nothing clashes", not asked and read(os.path.join(dest, "notes.txt")) == "archive notes")

    # the internet mark: on new files, never on the user's own kept file
    web = os.path.join(work, "web.zip")
    shutil.copy(arcs["zip"], web)
    m.mark_web(web, "https://example.com/web.zip")
    dest = os.path.join(work, "webout")
    os.makedirs(dest)
    open(os.path.join(dest, "notes.txt"), "w").write("MY NOTES")
    m.messagebox.askyesnocancel = lambda *x, **k: False
    m.extract_many(root, [web], dest, False, None)
    check("internet mark: new file marked, own kept file not",
          m.from_web(os.path.join(dest, "new.txt")) and not m.from_web(os.path.join(dest, "notes.txt")))
    root.destroy()

    # oddly named members found after extraction, both readers
    odd = os.path.join(work, "odd.zip")
    names = ["report: final?.txt", "dir end./a<b>.txt", "plain/ok.txt"]
    with zipfile.ZipFile(odd, "w") as zf:
        for n in names:
            zf.writestr(n, "x")
    for how in ("native", "engine"):
        d = os.path.join(work, "odd_" + how)
        if how == "native":
            m._extract_native(odd, "zip", d, None, None, noop, never)
        else:
            m.engine_extract(odd, d, None, None, noop, never)
        found = {n: m.extracted_path(d, n) for n in names}
        check(f"odd names found after {how} extraction", all(found.values()),
              ", ".join(f"{n} -> {os.path.relpath(p, d) if p else None}" for n, p in found.items()))
    check("missing member gives None", m.extracted_path(work, "nope/never.txt") is None)

    # zip comment kept
    c = os.path.join(work, "c.zip")
    with zipfile.ZipFile(c, "w") as zf:
        zf.comment = b"keep me"
        zf.writestr("a.txt", "a")
        zf.writestr("b.txt", "b")
    m.zip_delete(c, ["b.txt"])
    check("zip comment kept after delete", zipfile.ZipFile(c).comment == b"keep me")

    # clean-up: TEMP pointed at Downloads is refused; the Old downloads category still works
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    old = tempfile.tempdir
    tempfile.tempdir = downloads
    try:
        cats = {c["id"]: c for c in m.cleanup_categories()}
        temp_roots = [e.path for e in m._top_entries(cats["temp"])]
    finally:
        tempfile.tempdir = old
    check("Temporary files never touches Downloads", not any(p.startswith(downloads) for p in temp_roots),
          f"{len(temp_roots)} entries")
    dl = cats.get("downloads")
    check("Old downloads category still sees Downloads",
          dl is not None and m.expand_roots(dl["roots"], strict=False) == [downloads])
    normal = {c["id"]: c for c in m.cleanup_categories()}["temp"]
    check("Temporary files still works normally", m.expand_roots(normal["roots"]) == [tempfile.gettempdir()],
          str(m.expand_roots(normal["roots"])))
finally:
    shutil.rmtree(work, ignore_errors=True)
