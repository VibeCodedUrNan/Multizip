"""Probes for the recheck. Usage: recheck.py <src dir> <case>"""
import json, os, shutil, subprocess, sys, tempfile, zipfile

sys.path.insert(0, sys.argv[1])
import multizip as m

case = sys.argv[2]
noop = lambda n=1, label=None: None
never = lambda: False
work = tempfile.mkdtemp(prefix="mzre_")


def check(name, ok, detail=""):
    print(("[ok]   " if ok else "[FAIL] ") + name + (f"  ({detail})" if detail else ""))


try:
    if case == "cancel_replace":
        a, b = os.path.join(work, "a.txt"), os.path.join(work, "b.txt")
        for p in (a, b):
            open(p, "w").write("old " + os.path.basename(p))
        z = os.path.join(work, "t.zip")
        m.create_archive(z, [a, b], "zip", 6, None, noop, never)
        for p in (a, b):
            open(p, "w").write("new " + os.path.basename(p))
        calls = []

        def cancel_on_second():                       # cancel after the first new file is written
            calls.append(1)
            return len(calls) > 1
        try:
            m.add_into(z, [a, b], "", None, noop, cancel_on_second)
        except m.Cancelled:
            pass
        with zipfile.ZipFile(z) as zf:
            got = {n: zf.read(n).decode() for n in zf.namelist()}
        check("cancelled replace leaves the zip as it was", got == {"a.txt": "old a.txt", "b.txt": "old b.txt"},
              str(got))

    elif case == "zip_delete_stream":
        big = os.path.join(work, "big.bin")
        with open(big, "wb") as f:
            f.write(os.urandom(1024) * 64 * 1024)       # 64 MB
        z = os.path.join(work, "big.zip")
        with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
            zf.write(big, "big.bin")
            zf.writestr("gone.txt", "x")
            zf.writestr("dir/", "")
        import tracemalloc
        tracemalloc.start()
        m.zip_delete(z, ["gone.txt"])
        peak = tracemalloc.get_traced_memory()[1]
        with zipfile.ZipFile(z) as zf:
            names, ok = sorted(zf.namelist()), zf.testzip() is None
            same = zf.read("big.bin") == open(big, "rb").read()
        check("zip_delete keeps the rest intact", names == ["big.bin", "dir/"] and ok and same, str(names))
        check("zip_delete memory stays small", peak < 16 * 1024 * 1024, f"peak {peak // 1024 // 1024} MB")

    elif case == "junction":
        target = os.path.join(work, "target")
        os.makedirs(target)
        open(os.path.join(target, "precious.txt"), "w").write("x")
        j = os.path.join(work, "link")
        subprocess.run(["cmd", "/c", "mklink", "/J", j, target], capture_output=True, check=True)
        try:
            m.delete_permanently(j)
            check("junction removed", not os.path.lexists(j))
        except Exception as e:
            check("junction removed", False, f"{type(e).__name__}: {e}")
        check("junction target untouched", os.path.isfile(os.path.join(target, "precious.txt")))
        if os.path.lexists(j):
            os.rmdir(j)

    elif case == "bad_settings":
        os.environ["APPDATA"] = work
        os.makedirs(os.path.join(work, "MultiZip"))
        for bad in ({"custom": None}, {"level": "6"}, {"bg_dim": "x"}):
            with open(os.path.join(work, "MultiZip", "settings.json"), "w") as f:
                json.dump(bad, f)
            try:
                s = m.load_settings()
                m.level_name(s["level"])
                int(s["bg_dim"])
                check(f"settings {bad} load", True)
            except Exception as e:
                check(f"settings {bad} load", False, f"{type(e).__name__}: {e}")
        good = dict(theme="Mine", level=9, bg_dim=50, font_scale=1.15, caps=True, reveal=False, home_sort="Size",
                    custom={"Mine": dict(m.THEMES["Dark"])}, colors=dict(m.THEMES["Dark"]), dl_conns=4)
        with open(os.path.join(work, "MultiZip", "settings.json"), "w") as f:
            json.dump(good, f)
        s = m.load_settings()
        check("valid settings kept as saved", all(s[k] == v for k, v in good.items()),
              str({k: s[k] for k in good if s[k] != good[k]}))
finally:
    shutil.rmtree(work, ignore_errors=True)
