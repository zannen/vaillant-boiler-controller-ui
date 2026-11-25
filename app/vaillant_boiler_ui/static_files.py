"""The web UI's HTML, JS and CSS files."""

import os
import sys

# static/ sits next to this package
STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "static"
)
# URL path -> file in the static directory. Page shells and assets only, no
# boiler data, so no token needed: the page checks the token itself (auth.js)
# and sends you to /login.
STATIC_FILES = {
    "/": "index.html",
    "/login": "login.html",
    "/auth.js": "auth.js",
    "/app.js": "app.js",
    "/app.css": "app.css",
    "/login.js": "login.js",
    "/login.css": "login.css",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def load_static(static_dir):
    """URL path -> (content type, bytes) for every STATIC_FILES entry, read
    once at startup."""
    files = {}
    for url, name in STATIC_FILES.items():
        path = os.path.join(static_dir, name)
        try:
            with open(path, "rb") as f:
                files[url] = (
                    CONTENT_TYPES[os.path.splitext(name)[1]],
                    f.read(),
                )
        except OSError as e:
            sys.exit(f"cannot read web UI file {path}: {e}")
    return files
