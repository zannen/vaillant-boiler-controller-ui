import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time

try:
    import bcrypt
except ImportError:
    sys.exit(
        "vaillant-boiler-ui.py needs the bcrypt module: "
        "sudo apt install python3-bcrypt"
    )

from .base64url import b64url, unb64url
from .roles import ROLE_RANK, ROLES


class Auth:
    """Checks passwords against an htpasswd file, looks up roles, and
    issues/verifies HS256 JWTs.

    The user and role files are re-read whenever they change, so editing them
    needs no restart. Deleting a user or removing their role stops them
    refreshing; their current token still works until it expires (at most
    --token-hours)."""

    JWT_HEADER = b64url(
        json.dumps(
            {"alg": "HS256", "typ": "JWT"}, separators=(",", ":")
        ).encode()
    )
    # used for unknown users so a login takes as long whether or not the name
    # exists
    DUMMY_HASH = bcrypt.hashpw(b"x", bcrypt.gensalt(6))
    MAX_FAILURES, LOCKOUT_SECONDS = 5, 60

    def __init__(
        self,
        users_path,
        roles_path,
        secret_path,
        token_seconds,
        session_seconds,
    ):
        self.users_path, self.roles_path = users_path, roles_path
        self.token_seconds, self.session_seconds = (
            token_seconds,
            session_seconds,
        )
        self.lock = threading.Lock()
        self.users, self.roles, self.mtimes = {}, {}, None
        self.failures = {}  # client address -> (count, time of last failure)
        self.key = self._load_key(secret_path)
        self._reload()
        if not self.users:
            sys.exit(
                f"no usable bcrypt users in {users_path}; nobody could log in"
            )

    @staticmethod
    def _load_key(path):
        """Signing key, created on first run; kept so restarts don't log
        everyone out."""
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open(path, "rb") as f:
                key = f.read().strip()
            if len(key) < 32:
                sys.exit(
                    f"{path} is too short to be a signing key; "
                    "delete it to make a new one"
                )
            if os.stat(path).st_mode & 0o077:
                print(
                    f"warning: {path} is readable by other users; "
                    "chmod 600 it",
                    file=sys.stderr,
                )
            return key
        key = base64.b64encode(os.urandom(48))
        with os.fdopen(fd, "wb") as f:
            f.write(key + b"\n")
        print(f"created token signing key {path}", file=sys.stderr)
        return key

    def _reload(self):
        try:
            mtimes = (
                os.stat(self.users_path).st_mtime_ns,
                os.stat(self.roles_path).st_mtime_ns,
            )
        except OSError as e:
            print(f"cannot read user files: {e}", file=sys.stderr)
            return
        if mtimes == self.mtimes:
            return
        users = {}
        with open(self.users_path) as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                name, _, pw_hash = line.partition(":")
                pw_hash = pw_hash.split(":", 1)[0]
                if not pw_hash.startswith(("$2y$", "$2b$", "$2a$")):
                    print(
                        f"{self.users_path}:{n}: {name} skipped "
                        "(not a bcrypt hash; use htpasswd -B)",
                        file=sys.stderr,
                    )
                    continue
                users[name] = (
                    "$2b$" + pw_hash[4:]
                ).encode()  # $2y$ is the same algorithm as $2b$
        roles = {}
        with open(self.roles_path) as f:
            for n, line in enumerate(f, 1):
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                role, _, members = line.partition(":")
                role = role.strip()
                if role not in ROLES:
                    print(
                        f"{self.roles_path}:{n}: "
                        f"unknown role {role!r} ignored",
                        file=sys.stderr,
                    )
                    continue
                for name in members.split():
                    if (
                        ROLE_RANK.index(role)
                        > ROLE_RANK.index(roles.get(name, "viewer"))
                        or name not in roles
                    ):
                        roles[name] = role
        for name in users:
            if name not in roles:
                print(
                    f"{name} has no role in {self.roles_path}, "
                    "so cannot log in",
                    file=sys.stderr,
                )
        self.users, self.roles, self.mtimes = users, roles, mtimes
        print(f"loaded {len(users)} users", file=sys.stderr)

    def role_of(self, name):
        with self.lock:
            self._reload()
            return self.roles.get(name) if name in self.users else None

    def login(self, name, password, client):
        """Returns (token info, None) or (None, (http code, message))."""
        now = time.time()
        with self.lock:
            count, last = self.failures.get(client, (0, 0))
            if (
                count >= self.MAX_FAILURES
                and now - last < self.LOCKOUT_SECONDS
            ):
                return None, (
                    429,
                    f"too many failed logins; wait {self.LOCKOUT_SECONDS} s",
                )
            self._reload()
            pw_hash = self.users.get(name)
            role = self.roles.get(name)
        try:
            ok = (
                bcrypt.checkpw(password.encode(), pw_hash or self.DUMMY_HASH)
                and pw_hash is not None
            )
        except ValueError:  # malformed hash in the file
            ok = False
        with self.lock:
            if not ok:
                self.failures[client] = (
                    count + 1 if now - last < self.LOCKOUT_SECONDS else 1,
                    now,
                )
                return None, (401, "wrong username or password")
            self.failures.pop(client, None)
        if role is None:
            return None, (403, "this user has no role; ask the admin")
        return self.issue(name, role, int(now)), None

    def issue(self, name, role, auth_time):
        now = int(time.time())
        exp = min(now + self.token_seconds, auth_time + self.session_seconds)
        claims = {
            "sub": name,
            "role": role,
            "iat": now,
            "exp": exp,
            "auth_time": auth_time,
        }
        body = (
            self.JWT_HEADER
            + "."
            + b64url(json.dumps(claims, separators=(",", ":")).encode())
        )
        sig = b64url(
            hmac.new(self.key, body.encode(), hashlib.sha256).digest()
        )
        return {
            "token": body + "." + sig,
            "expiresIn": exp - now,
            "user": name,
            "role": role,
        }

    def verify(self, token):
        """Claims of a valid, unexpired token, or None."""
        try:
            header, payload, sig = token.split(".")
            if (
                header != self.JWT_HEADER
            ):  # only ever accept our own HS256 header
                return None
            want = hmac.new(
                self.key, f"{header}.{payload}".encode(), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(unb64url(sig), want):
                return None
            claims = json.loads(unb64url(payload))
        except (ValueError, TypeError):
            return None
        if not isinstance(claims, dict) or claims.get("role") not in ROLES:
            return None
        if time.time() >= claims.get("exp", 0):
            return None
        return claims

    def refresh(self, claims):
        """New token for the same login session, re-checking the user still
        exists and their role."""
        role = self.role_of(claims["sub"])
        if role is None:
            return None, (401, "user no longer allowed")
        if time.time() >= claims["auth_time"] + self.session_seconds:
            return None, (401, "session too old; log in again")
        return self.issue(claims["sub"], role, claims["auth_time"]), None
