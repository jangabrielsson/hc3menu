"""py2app build script.

Usage:
    python setup.py py2app -A   # alias build (fast, dev)
    python setup.py py2app      # full standalone .app
"""
from setuptools import setup

APP = ["run_hc3menu.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "HC3 Menu",
        "CFBundleIdentifier": "com.example.hc3menu",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,  # menu-bar only, no Dock icon
        "NSHighResolutionCapable": True,
    },
    "packages": ["rumps", "requests", "dotenv"],
    "includes": ["hc3menu"],
    # Uncomment when you provide an .icns file:
    # "iconfile": "assets/icon.icns",
}

setup(
    app=APP,
    name="HC3 Menu",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
