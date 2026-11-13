# Third-party components

This application uses unmodified third-party packages. The Python application source and build instructions are included in the source archive. The binary distribution uses dynamically loaded Qt/PySide6 libraries in `_internal`; these libraries can be replaced with compatible builds. No restriction on debugging modifications of these libraries is imposed by this application.

Package metadata and license files shipped by the installed distributions are copied into `licenses/` when running `package_release.py`.

| Component | Version | Upstream |
|---|---|---|
| Python | 3.12.14 | https://www.python.org/downloads/source/ |
| Telethon | 1.45.0 | https://github.com/LonamiWebs/Telethon |
| PySide6 Essentials / Qt for Python | 6.12.0 | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| Shiboken6 | 6.12.0 | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| Qt | bundled with PySide6 | https://code.qt.io/cgit/qt/ |
| pyaes | 1.6.1 | https://github.com/ricmoo/pyaes |
| rsa | 4.9.1 | https://github.com/sybrenstuvel/python-rsa |
| pyasn1 | 0.6.4 | https://github.com/pyasn1/pyasn1 |
| PyInstaller bootloader | 6.22.3 | https://github.com/pyinstaller/pyinstaller |

The installed PySide6 distribution declares `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` in its metadata. See the Qt licensing and source distribution pages: https://www.qt.io/licensing/open-source-lgpl-obligations and https://download.qt.io/official_releases/ . Original notices remain applicable. This document does not relicense third-party software.
