from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from aggregator.models import Outlet
from aggregator.outlet_prefs import (
    VALID_PREFERENCES,
    outlets_by_preference,
    set_outlet_preference,
    unrated_outlets_recent,
)

prefs = Blueprint("prefs", __name__)


@prefs.route("/settings")
@prefs.route("/settings/sources")
@login_required
def settings_sources():
    unrated = unrated_outlets_recent(current_user, days=14, limit=50)
    preferred = outlets_by_preference(current_user, "prefer")
    allowed = outlets_by_preference(current_user, "allow")
    muted = outlets_by_preference(current_user, "mute")
    return render_template(
        "settings.html",
        unrated_outlets=unrated,
        preferred_outlets=preferred,
        allowed_outlets=allowed,
        muted_outlets=muted,
    )


@prefs.route("/prefs/outlets/<int:outlet_id>", methods=["POST"])
@login_required
def set_outlet_pref(outlet_id):
    Outlet.query.get_or_404(outlet_id)
    preference = (request.form.get("preference") or "").strip().lower()
    next_url = request.form.get("next") or request.referrer or url_for("prefs.settings_sources")

    try:
        if preference in ("clear", "unrated", ""):
            set_outlet_preference(current_user, outlet_id, None)
            flash("Source marked unrated.", "success")
        elif preference in VALID_PREFERENCES:
            set_outlet_preference(current_user, outlet_id, preference)
            flash(f"Source set to {preference}.", "success")
        else:
            flash("Unknown preference action.", "error")
    except ValueError as exc:
        flash(str(exc), "error")

    # Avoid open redirects
    if not next_url.startswith("/"):
        next_url = url_for("prefs.settings_sources")
    return redirect(next_url)


@prefs.route("/prefs/outlets/reset", methods=["POST"])
@login_required
def reset_all_prefs():
    from aggregator import db
    from aggregator.models import UserOutletPreference

    UserOutletPreference.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    flash("All source preferences cleared.", "success")
    return redirect(url_for("prefs.settings_sources"))
