"""Roles and their permissions."""

# role -> permissions. Every API endpoint names the permission it needs.
ROLES = {
    "viewer": {"read"},
    "user": {"read", "control"},
    "admin": {"read", "control", "admin"},
}
ROLE_RANK = ["viewer", "user", "admin"]
