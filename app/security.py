
import secrets
from functools import wraps

from flask import abort, flash, redirect, request, session, url_for


ALL_ROLES = {"admin", "supervisor", "worker"}

CAN = {
    "admin": {
        "batch", "collection", "complaint",
        "vehicle", "master", "users",
    },
    "supervisor": {
        "batch", "collection", "complaint", "vehicle",
    },
    "worker": {"collection", "complaint"},
    "citizen": set(),
}

PAGES = [
    ("overview", "Overview", ALL_ROLES),
    ("batches", "Batches", ALL_ROLES),
    ("collections", "Collections", ALL_ROLES),
    ("complaints", "Complaints", ALL_ROLES),
    ("bins", "Bins", ALL_ROLES),
    ("vehicles", "Vehicles", ALL_ROLES),
    ("workers", "Workers", ALL_ROLES),
    ("zones", "Zones", {"admin", "supervisor"}),
    ("stations", "Stations", {"admin", "supervisor"}),
    ("users", "Users", {"admin"}),
]

PAGE_ROLES = {pid: roles for pid, _label, roles in PAGES}


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    if not sent or not expected or not secrets.compare_digest(
        sent, expected
    ):
        abort(
            400,
            description="Your form has expired. Reload the page and try again.",
        )


def can(action):
    user = session.get("user")
    return bool(user) and action in CAN.get(user["role"], set())


def nav_items():
    user = session.get("user")
    if not user:
        return []

    if user.get("role") == "citizen":
        return [{
            "endpoint": "auth.citizen_home",
            "label": "My Citizen Dashboard",
        }]

    return [
        {"endpoint": f"views.{pid}", "label": label}
        for pid, label, roles in PAGES
        if user["role"] in roles
    ]


def requires(action=None, page=None):
    """Protect municipal pages and actions by role."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = session.get("user")

            if not user:
                return redirect(
                    url_for("auth.login", next=request.path)
                )

            if request.method == "POST":
                check_csrf()

            # Citizens must use their dedicated citizen routes.
            if user.get("role") == "citizen":
                flash(
                    "You do not have access to that page.",
                    "error",
                )
                return redirect(url_for("auth.citizen_home"))

            if page and user["role"] not in PAGE_ROLES.get(
                page, set()
            ):
                flash("You do not have access to that page.", "error")
                return redirect(url_for("views.overview"))

            if action and action not in CAN.get(
                user["role"], set()
            ):
                flash("Your role cannot make this change.", "error")
                return redirect(url_for("views.overview"))

            return view(*args, **kwargs)

        return wrapped

    return decorator
