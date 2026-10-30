# Third-party software

MultiZip's own code is licensed under the GNU General Public License v3.0 (see `LICENSE`).

The source code in this repository contains no third-party code. The **ready-made programs** (the Windows
`MultiZip.exe`, the Linux `MultiZip` and the macOS `MultiZip.app` that `build.bat` / `build.sh` produce) bundle
the components below. Each keeps its own licence. All of them can be combined with GPL-3.0 software; 7-Zip is
included as a separate program that MultiZip starts, not linked into MultiZip's code.

| Component | Licence | Included | Source |
|---|---|---|---|
| **7-Zip** (`7z.exe` + `7z.dll`, or `7zz`) by Igor Pavlov | GNU LGPL 2.1 or later, with the unRAR licence restriction; some parts BSD 3-clause / BSD 2-clause. Full text: [`licenses/7-Zip-License.txt`](licenses/7-Zip-License.txt) | when 7-Zip was found or downloaded at build time | https://www.7-zip.org |
| Python | Python Software Foundation License | always (the program runs on it) | https://www.python.org |
| Tcl/Tk | Tcl/Tk licence (BSD-style) | always (the window toolkit) | https://www.tcl-lang.org |
| OpenSSL | Apache License 2.0 | always, with Python (secure downloads) | https://www.openssl.org |
| libffi | MIT | always, with Python | https://sourceware.org/libffi/ |
| PyInstaller bootloader | GPL 2.0 or later with the bootloader exception, which allows bundled programs under any licence | always | https://pyinstaller.org |
| Pillow | MIT-CMU (HPND) | always | https://python-pillow.org |
| tkinterdnd2 | MIT | when installed at build time | https://github.com/Eliav2/tkinterdnd2 |
| tkdnd (inside tkinterdnd2) | BSD-style, see its project | with tkinterdnd2 | https://github.com/petasis/tkdnd |
| rarfile | ISC | when installed at build time | https://github.com/markokr/rarfile |
| py7zr | GNU LGPL 2.1 or later | only when version 0.20.2 or newer could be installed (older versions are left out) | https://github.com/miurahr/py7zr |
| pycryptodome | BSD 2-clause and Unlicense | with py7zr or rarfile, when installed | https://www.pycryptodome.org |
| texttable | MIT | with py7zr | https://github.com/foutaise/texttable |
| zstandard | BSD 3-clause | only when built with Python 3.13 or older | https://github.com/indygreg/python-zstandard |

## 7-Zip and RAR

7-Zip's licence asks that binary redistributions reproduce its licence information: that is
[`licenses/7-Zip-License.txt`](licenses/7-Zip-License.txt), which must ship alongside every release that
contains 7-Zip. Its RAR code comes with this restriction from the unRAR licence:

> The unRAR sources cannot be used to re-create the RAR compression algorithm, which is proprietary.

MultiZip only reads RAR archives; it never creates them.
