import json
import queue
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

from .auth import Auth
from .heating_controller import HeatingController
from .hub import Hub
from .roles import ROLES
from .schedule_rules import DAY_KEYS, MAX_PERIODS, validate_schedule
from .util import log

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'none'"
    ),
}


class RequestHandler(BaseHTTPRequestHandler):
    """Serves the web UI and its JSON API. serve() makes a subclass that sets
    the class attributes below."""

    hub: Hub
    auth: Auth
    controller: HeatingController
    static: dict  # URL path -> (content type, bytes)
    started: float  # time.time() when the server started

    # path -> (method name, permission needed); None = no token needed.
    # serve() adds a GET route for each static file.
    API_ROUTES = {
        ("GET", "/health"): ("_health", None),
        ("POST", "/api/login"): ("_login", None),
        ("POST", "/api/refresh"): ("_refresh", "read"),
        ("GET", "/api/whoami"): ("_whoami", "read"),
        ("GET", "/api/state"): ("_state", "read"),
        ("GET", "/api/agg"): ("_agg", "read"),
        ("GET", "/api/commands"): ("_commands", "read"),
        ("GET", "/api/events"): ("_events", "read"),
        ("GET", "/api/schedule"): ("_get_schedule", "read"),
        ("PUT", "/api/schedule"): ("_put_schedule", "admin"),
        ("POST", "/api/override"): ("_override", "control"),
    }
    ROUTES: dict = API_ROUTES

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def _dispatch(self, method):
        path, _, self.query = self.path.partition("?")
        route = self.ROUTES.get((method, path))
        if route is None:
            allowed = any(p == path for _, p in self.ROUTES)
            self._json(
                405 if allowed else 404,
                {"error": ("method not allowed" if allowed else "not found")},
            )
            return
        handler, permission = route
        self.claims = None
        if permission is not None:
            header = self.headers.get("Authorization", "")
            self.claims = (
                self.auth.verify(header[7:])
                if header.startswith("Bearer ")
                else None
            )
            if self.claims is None:
                self._json(
                    401,
                    {"error": "login required"},
                    {"WWW-Authenticate": "Bearer"},
                )
                return
            if permission not in ROLES[self.claims["role"]]:
                self._json(
                    403,
                    {
                        "error": f"your role ({self.claims['role']}) "
                        "cannot do this"
                    },
                )
                return
        getattr(self, handler)()

    def _send(self, code, ctype, body, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj, headers=None):
        self._send(
            code,
            "application/json",
            json.dumps(obj, separators=(",", ":")).encode(),
            headers,
        )

    def _body(self, limit=4096):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if not 0 < n <= limit:
            return None
        try:
            obj = json.loads(self.rfile.read(n))
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    def _static(self):
        self._send(200, *self.static[self.path.partition("?")[0]])

    def _health(self):
        with self.hub.lock:
            connected, updated = self.hub.connected, self.hub.updated
        self._json(
            200,
            {
                "status": "ok",
                "uptime": int(time.time() - self.started),
                "ebusd": "connected" if connected else "disconnected",
                "lastUpdate": updated,
            },
        )

    def _login(self):
        body = self._body()
        name, password = (body or {}).get("username"), (body or {}).get(
            "password"
        )
        if (
            not isinstance(name, str)
            or not isinstance(password, str)
            or not name
            or len(password) > 200
        ):
            self._json(400, {"error": "username and password required"})
            return
        info, err = self.auth.login(name, password, self.client_address[0])
        if err:
            log(
                f"login failed for {name!r} from "
                f"{self.client_address[0]}: {err[1]}"
            )
            self._json(err[0], {"error": err[1]})
        else:
            self._json(200, info)

    def _refresh(self):
        info, err = self.auth.refresh(self.claims)
        (
            self._json(err[0], {"error": err[1]})
            if err
            else self._json(200, info)
        )

    def _whoami(self):
        c = self.claims
        self._json(
            200,
            {
                "user": c["sub"],
                "role": c["role"],
                "permissions": sorted(ROLES[c["role"]]),
                "expiresIn": int(c["exp"] - time.time()),
            },
        )

    def _schedule_reply(self, code=200):
        with self.hub.lock:
            status = self.hub.control
        self._json(
            code,
            {
                **self.controller.schedule.get(),
                "maxPeriods": MAX_PERIODS,
                "dayOrder": DAY_KEYS,
                "status": status,
            },
        )

    def _get_schedule(self):
        self._schedule_reply()

    def _put_schedule(self):
        body = self._body(limit=32768)
        if body is None:
            self._json(400, {"error": "expected a JSON schedule"})
            return
        checked, err = validate_schedule(body)
        if err:
            self._json(400, {"error": err})
            return
        try:
            self.controller.schedule.set(checked, self.claims["sub"])
        except OSError as e:
            self._json(500, {"error": f"cannot save schedule: {e}"})
            return
        log(
            f"schedule saved by {self.claims['sub']} "
            f"(enabled: {checked['enabled']})"
        )
        self.controller.wake.set()  # apply it now
        time.sleep(0.2)  # usually lets the status below reflect the new one
        self._schedule_reply()

    def _override(self):
        user = self.claims["sub"]
        log(f"heating override by {user}")
        self.controller.request_override(user)
        time.sleep(0.5)  # usually long enough for the send to finish
        self._schedule_reply()

    def _state(self):
        with self.hub.lock:
            snap = self.hub.snapshot()
        self._json(200, snap)

    def _commands(self):
        self._json(
            200,
            {
                "rows": self.controller.commands.get(),
                "holdSeconds": self.controller.HOLD_SECONDS,
            },
        )

    def _agg(self):
        tier = urllib.parse.parse_qs(self.query).get("tier", [""])[0]
        data = self.hub.agg_rows(tier)
        if data is None:
            self._json(404, {"error": "unknown tier"})
        else:
            self._json(200, data)

    def _push(self, msg):
        self.wfile.write(
            b"data: "
            + json.dumps(msg, separators=(",", ":")).encode()
            + b"\n\n"
        )
        self.wfile.flush()

    def _events(self):
        q, snap = self.hub.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self._push(snap)
            # the token was checked when the stream opened; end it when the
            # token expires, and the page reconnects with its refreshed token
            while (
                not getattr(q, "dropped", False)
                and time.time() < self.claims["exp"]
            ):
                try:
                    self._push(q.get(timeout=5))
                except (
                    queue.Empty
                ):  # a real message, so the browser's watchdog sees it
                    self._push({"type": "ping"})
            self.close_connection = True
        except OSError:
            pass  # browser went away
        finally:
            self.hub.unsubscribe(q)

    def log_message(self, *args):
        pass
