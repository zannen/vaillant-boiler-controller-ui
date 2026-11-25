"""The web server."""

import threading
import time
from http.server import ThreadingHTTPServer

from .request_handler import RequestHandler


def serve(hub, auth, controller, static, bind, port):
    """Start the web server in a background thread."""
    handler = type(
        "Handler",
        (RequestHandler,),
        {
            "hub": hub,
            "auth": auth,
            "controller": controller,
            "static": static,
            "started": time.time(),
            "ROUTES": {
                **{("GET", url): ("_static", None) for url in static},
                **RequestHandler.API_ROUTES,
            },
        },
    )
    httpd = ThreadingHTTPServer((bind, port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
