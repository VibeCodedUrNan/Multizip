"""Run every MultiZip check and summarise.

Each check script prints one line per check: [ok] or [FAIL] ([safe] or [VULN] for the security checks).
Needs Windows with 7-Zip installed, and:  pip install pillow py7zr tkinterdnd2
Linux and macOS behaviour is checked in simulation. To test a built MultiZip.exe as well, run smoke_exe.ps1.

    python tests/run_all.py
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNS = [("check_archives.py",), ("check_engine.py",), ("check_security.py",), ("check_extract_overwrite.py",),
        ("check_icon_log.py",), ("check_linux_macos.py",), ("check_linux_integration.py",), ("check_scripts.py",)]
RUNS += [("check_regressions.py", c) for c in ("diskpart", "cli", "cli_iso", "cli_crash", "password", "zipadd", "fmt")]
RUNS += [("check_regressions2.py", c) for c in ("junction", "bad_settings", "cancel_replace", "zip_delete_stream")]


def main():
    passed, failed = 0, []
    for script, *args in RUNS:
        name = " ".join([script] + args)
        p = subprocess.run([sys.executable, "-B", os.path.join(HERE, script), ROOT] + args,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        out = p.stdout + p.stderr
        ok = len(re.findall(r"^\[(?:ok|safe)\]", out, re.M))
        bad = re.findall(r"^\[(?:FAIL|VULN)\].*$", out, re.M)
        if p.returncode or "Traceback" in out:
            bad.append(f"crashed (exit code {p.returncode}):\n" + out.strip()[-1500:])
        passed += ok
        failed += [f"{name}: {b}" for b in bad]
        print(f"{'FAIL' if bad else 'ok  '}  {name:45} {ok} passed" + (f", {len(bad)} failed" if bad else ""))
    print(f"\n{passed} checks passed, {len(failed)} failed")
    for f in failed:
        print("  " + f)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
