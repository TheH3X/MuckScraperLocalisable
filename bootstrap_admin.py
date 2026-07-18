import os
import sys
import time

from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from flask_migrate import stamp, upgrade

from aggregator import db
from aggregator.app import app
from aggregator.models import User


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set in .env")
    return value


def _wait_for_database():
    last_error = None
    for attempt in range(1, 31):
        try:
            with app.app_context():
                db.session.execute(db.text("SELECT 1"))
            return
        except OperationalError as exc:
            last_error = exc
            print(f"Database not ready yet, retrying ({attempt}/30)...")
            time.sleep(2)
    raise RuntimeError(f"Database did not become ready: {last_error}")


def _init_schema():
    """Build or update the schema, keeping Alembic's version table honest.

    A genuinely empty database gets its schema built from the current models
    and is stamped as current — there's nothing for Alembic to apply. An
    existing database is never touched with db.create_all(): that only
    creates tables it doesn't already recognize and silently skips ALTER
    TABLE changes on tables that already exist, which would desync it from
    a bare `stamp`. Its pending migrations are applied for real instead.
    """
    with app.app_context():
        db.session.execute(db.text("CREATE EXTENSION IF NOT EXISTS vector"))
        db.session.commit()

        is_fresh_database = not inspect(db.engine).get_table_names()
        if is_fresh_database:
            db.create_all()
            stamp(revision="head")
        else:
            upgrade()


def bootstrap_admin():
    username = required_env("ADMIN_USERNAME")
    email = required_env("ADMIN_EMAIL")
    password = required_env("ADMIN_PASSWORD")

    _wait_for_database()
    _init_schema()

    with app.app_context():
        user = User.query.filter_by(username=username).first()
        email_owner = User.query.filter_by(email=email).first()

        if email_owner and email_owner.username != username:
            raise RuntimeError(
                f"ADMIN_EMAIL is already used by user '{email_owner.username}'. "
                "Choose a different ADMIN_EMAIL or update that user manually."
            )

        if user:
            user.email = email
            user.is_admin = True
            user.set_password(password)
            action = "updated"
        else:
            user = User(username=username, email=email, is_admin=True)
            user.set_password(password)
            db.session.add(user)
            action = "created"

        db.session.commit()
        print(f"Admin user '{username}' {action}.")

        from seed_topics import run as seed_topics
        seed_topics()
        print("Topics seeded.")


if __name__ == "__main__":
    try:
        bootstrap_admin()
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)
