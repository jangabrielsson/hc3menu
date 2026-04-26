"""py2app build script for HC3 Menu.

Usage:
    python setup.py py2app -A   # alias build (fast, dev only — needs source tree)
    python setup.py py2app      # full standalone .app (for distribution)
"""
from setuptools import setup

# Single source of truth for the version.
exec(open("hc3menu/__version__.py").read())  # defines __version__  # noqa: S102

APP = ["run_hc3menu.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "HC3 Menu",
        "CFBundleDisplayName": "HC3 Menu",
        "CFBundleIdentifier": "com.jangabrielsson.hc3menu",
        "CFBundleShortVersionString": __version__,  # noqa: F821
        "CFBundleVersion": __version__,  # noqa: F821
        "LSUIElement": True,                 # menu-bar only, no Dock icon
        "LSMinimumSystemVersion": "11.0",    # Big Sur+ (SF Symbols)
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "© 2026 Jan Gabrielsson. MIT License.",
    },
    "packages": ["rumps", "requests", "dotenv", "certifi", "urllib3",
                 "charset_normalizer", "idna"],
    "includes": ["hc3menu"],
    "iconfile": "assets/icon.icns",
}

setup(
    app=APP,
    name="HC3 Menu",
    version=__version__,  # noqa: F821
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
