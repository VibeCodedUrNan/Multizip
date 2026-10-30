import os, sys, tempfile
sys.path.insert(0, sys.argv[1])
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="mzicon_")
import multizip as m

log = os.path.join(os.environ["APPDATA"], "MultiZip", "icons.log")
a = m.IconCache()
a.log("k1", "first")
a.log("k2", "second")
n1 = len(open(log, encoding="utf-8").readlines())
b = m.IconCache()                       # a new session starts the log fresh
b.log("k3", "third")
n2 = len(open(log, encoding="utf-8").readlines())
print(("[ok]   " if (n1, n2) == (2, 1) else "[FAIL] ") + f"icons.log per session  (lines: {n1} then {n2})")

root = m.tk.Tk()
root.withdraw()
ic = m.icon_for(sys.executable, False, 16), m.icon_for(os.environ["SystemRoot"], True, 48)
print(("[ok]   " if all(ic) else "[FAIL] ") + "real icons still load  (16px file, 48px folder)")
print(("[ok]   " if not os.path.exists(log) or "px icon" not in open(log, encoding="utf-8").read() else "[FAIL] ")
      + "no size-diagnostic lines written")
root.destroy()
