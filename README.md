# MultiZip

<img src="multizip.png" width="96" align="right" alt="MultiZip icon">

A friendly archive manager for Windows, with Linux and macOS support. Open, create, extract and test
zip, 7z, rar, tar and many more formats, plus a few disk tools.

## Features

- **Archives like folders.** Double-click an archive to browse it. Extract all of it or just what you select,
  drag files out, drop files in, test it, delete files inside a zip.
- **Create archives** as zip, 7z, tar, tar.gz, tar.bz2, tar.xz or tar.zst, with passwords for 7z and zip
  (AES-256).
- **About 70 formats to open** with the 7-Zip engine: RAR, ISO, CAB, WIM, VHD/VHDX, DMG, DEB, RPM, ARJ, LZH and
  more. Help > Supported formats can download the engine if it is missing.
- **Safe extraction.** Archives cannot write outside the folder you chose. MultiZip asks before replacing your
  files, warns about "zip bombs" and disk space, and files from a downloaded archive keep Windows' "downloaded
  from the internet" warning.
- **Right-click menu**: Extract Here, Extract to folder, Add to archive and more, in Explorer and in the main
  Linux file managers (Files, Dolphin, Thunar, Nemo, Caja).
- **Tools**: disk usage analyser, clean-up assistant, download manager (pause, resume, several connections,
  checksums), bootable USB maker, download manager, data recovery and more 
- Your Desktop as a home screen, drag and drop everywhere, themes and background pictures.

## Install

**Windows:** download `MultiZip-windows.zip` from [Releases](../../releases), unzip it and run `MultiZip.exe`.
No install is needed. To add the right-click menu and file types, build the installer yourself (below). The
program is not code-signed, so Windows SmartScreen may say "Windows protected your PC": choose
**More info > Run anyway**.

**Linux:** build it yourself with `bash build.sh` (below), then run `bash install-linux.sh`. That installs it
for your user only (no root), with an applications-menu entry, "Open with" for archives and right-click menus.
`build.sh` also makes `MultiZip-linux-<arch>.tar.gz`, which you can give to others. Remove it with
`bash ~/.local/lib/multizip/install-linux.sh --uninstall`.

**macOS:** build it yourself (below), then drag `MultiZip.app` into Applications. It is not signed by Apple:
the first time, right-click it and choose Open.

## Build it yourself

You need Python 3 with Tk. **Python 3.13 is recommended**: on newer versions the 7z library py7zr is only
available in an old version with a known security bug, so the build leaves it out and 7z files go through the
7-Zip engine instead.

- **Windows:** install Python 3.13 from [python.org](https://www.python.org/downloads/) (tick "tcl/tk" and
  "Add python.exe to PATH"), then double-click `build.bat`. Result: `dist\MultiZip.exe`. With
  [Inno Setup 6](https://jrsoftware.org/isdl.php) installed it also makes `Output\MultiZip-Setup.exe`.
- **Linux / macOS:** `bash build.sh`. Result: `dist/MultiZip` plus a `.tar.gz` to hand out (Linux), or
  `dist/MultiZip.app` (macOS). The top of `build.sh` lists the packages to install first.

Both builds use a private virtual environment, so your Python installation is not touched. If 7-Zip is
installed (Windows: 7-Zip; Linux/macOS: the `7zz` program), it is bundled into the program.

**Run without building:**

```
pip install pillow py7zr tkinterdnd2
python multizip.py
```

On Python 3.14 and newer, pip may only find a py7zr older than 0.20.2. MultiZip ignores that version
because of its security bug, and opens 7z files with 7-Zip instead.

## Command line

```
multizip <archive or folder>        open it
multizip x  <archives...>           extract each into a folder named after it
multizip xh <archives...>           extract here
multizip xd <archives...>           extract, asking where to
multizip t  <archives...>           test
multizip a  <files and folders...>  add to a new archive
```

## Status

Windows is the main platform and is tested. The Linux and macOS support has so far only been tested in
simulation; please [open an issue](../../issues) if something does not work there.

## Good to know

- The **bootable USB maker erases the drive you pick**. It only lists USB and SD drives, never the system
  disk, and you must type ERASE to continue.
- The **clean-up assistant deletes** temporary files and caches for good (old downloads go to the Recycle Bin),
  but only after showing what it found and asking you.
- Archive **passwords are handed to 7-Zip on its command line**. On a computer shared with other users, they
  could see it in the list of running programs while 7-Zip works.

## Tests

On Windows with 7-Zip installed and `pip install pillow py7zr tkinterdnd2`:

```
python tests/run_all.py
```

## License

MultiZip is free software under the [GNU General Public License v3.0](LICENSE).
The programs built from it include other open-source software, listed in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). 7-Zip is copyright Igor Pavlov; its RAR code may not be used
to create RAR archives.
