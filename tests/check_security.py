"""Can a crafted archive write outside the folder it is extracted to, via a symbolic link inside it?
Classic attack: entry 1 is a link 'evil' -> <outside folder>; entry 2 is a file 'evil/pwned.txt'.
Usage: symlink_attack.py <src>"""
import io, os, shutil, sys, tarfile, tempfile, zipfile
import py7zr                        # builds the test archives, whatever version MultiZip itself accepts

sys.path.insert(0, sys.argv[1])
import multizip as m

noop = lambda n=1, label=None: None
never = lambda: False
work = tempfile.mkdtemp(prefix="mzsec_")
outside = os.path.join(work, "OUTSIDE")
os.makedirs(outside)


def check(name, safe, detail=""):
    print(("[safe] " if safe else "[VULN] ") + name + (f"  ({detail})" if detail else ""))


try:
    try:
        os.symlink(outside, os.path.join(work, "probe"), target_is_directory=True)
        os.remove(os.path.join(work, "probe")) if os.path.isfile(os.path.join(work, "probe")) else os.rmdir(os.path.join(work, "probe"))
        can_link = True
    except OSError as e:
        can_link = False
        print(f"note: this Windows account cannot create symbolic links ({e.strerror}); "
              "the link step is then refused by Windows itself")
    print("python", sys.version.split()[0], "| tar data filter:", hasattr(tarfile, "data_filter"),
          "| py7zr", py7zr.__version__, "(used by MultiZip:", m.py7zr is not None, ")", "| can create symlinks:", can_link)

    # --- tar: symlink then a file through it (and an absolute-path link)
    t = os.path.join(work, "evil.tar")
    with tarfile.open(t, "w") as tf:
        li = tarfile.TarInfo("evil")
        li.type, li.linkname = tarfile.SYMTYPE, outside
        tf.addfile(li)
        data = b"pwned"
        fi = tarfile.TarInfo("evil/pwned.txt")
        fi.size = len(data)
        tf.addfile(fi, io.BytesIO(data))
    dest = os.path.join(work, "x_tar")
    try:
        m.extract(t, dest, None, None, noop, never, mark=False)
        err = ""
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    check("tar with a symlink escape", not os.path.exists(os.path.join(outside, "pwned.txt")), err[:120])

    # --- tar hardlink to an outside file, then overwrite it
    victim = os.path.join(outside, "victim.txt")
    open(victim, "w").write("original")
    t2 = os.path.join(work, "evil2.tar")
    with tarfile.open(t2, "w") as tf:
        li = tarfile.TarInfo("h")
        li.type, li.linkname = tarfile.LNKTYPE, victim
        tf.addfile(li)
        fi = tarfile.TarInfo("h")
        fi.size = 5
        tf.addfile(fi, io.BytesIO(b"PWNED"))
    try:
        m.extract(t2, os.path.join(work, "x_tar2"), None, None, noop, never, mark=False)
        err = ""
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    check("tar with a hardlink escape", open(victim).read() == "original", err[:120])

    # --- zip and 7z: names with ../ and absolute paths (the plain path-traversal attack)
    for fmt in ("zip", "7z"):
        a = os.path.join(work, "dotdot." + fmt)
        payload = os.path.join(work, "payload.txt")
        open(payload, "w").write("x")
        if fmt == "zip":
            with zipfile.ZipFile(a, "w") as zf:
                zf.writestr("../../OUTSIDE/zipslip.txt", "pwned")
        else:
            with py7zr.SevenZipFile(a, "w") as z:
                z.write(payload, "../../OUTSIDE/zipslip.txt")
        try:
            m.extract(a, os.path.join(work, "x_" + fmt, "deep"), None, None, noop, never, mark=False)
            err = ""
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        check(f"{fmt} with ../ names", not os.path.exists(os.path.join(outside, "zipslip.txt")), err[:120])

    # --- 7z via py7zr with a symlink entry, then a file through it (py7zr < 0.20.2 is known-vulnerable)
    if can_link:
        srcdir = os.path.join(work, "s")
        os.makedirs(srcdir)
        lnk = os.path.join(srcdir, "evil")
        os.symlink(outside, lnk, target_is_directory=True)
        a = os.path.join(work, "evil.7z")
        with py7zr.SevenZipFile(a, "w") as z:
            z.write(lnk, "evil")
            z.write(payload, "evil/pwned7.txt")
        for how in ("py7zr", "7-Zip engine"):
            dest = os.path.join(work, "x_7z_" + how[:4])
            try:
                if how == "py7zr":
                    m.extract(a, dest, None, None, noop, never, mark=False)
                else:
                    m.engine_extract(a, dest, None, None, noop, never)
                err = ""
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            hit = os.path.exists(os.path.join(outside, "pwned7.txt"))
            check(f"7z with a symlink escape ({how})", not hit, err[:120])
            if hit:
                os.remove(os.path.join(outside, "pwned7.txt"))
finally:
    shutil.rmtree(work, ignore_errors=True)
