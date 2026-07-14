from functools import wraps

from flask import abort, flash, redirect, url_for
from flask_login import current_user, login_required


def admin_required(view):
    """Require an authenticated admin user."""

    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not getattr(current_user, "is_admin", False):
            flash("Admin access required.", "error")
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def login_redirect_endpoint():
    """Where to send a user after a successful login."""
    if getattr(current_user, "is_admin", False):
        return "admin.list_articles"
    return "public.latest_edition"
