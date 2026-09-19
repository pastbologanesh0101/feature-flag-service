import os

from flask import Flask


def create_app(testing: bool = False, database: str | None = None):
    """Application factory.

    Args:
        testing: when True, use a private in-memory SQLite database and
            enable Flask's TESTING mode. Ideal for unit tests -- each
            ``create_app(testing=True)`` call gets a fresh, isolated DB.
        database: explicit sqlite path/URI override (mainly for advanced
            test scenarios). Ignored when ``testing`` is True.
    """
    app = Flask(__name__)
    app.extensions = getattr(app, "extensions", {}) or {}

    if testing:
        app.config["DATABASE"] = ":memory:"
        app.config["TESTING"] = True
    else:
        app.config["DATABASE"] = database or os.environ.get(
            "FFS_DATABASE", os.path.join(os.getcwd(), "feature_flags.db")
        )

    from . import db

    db.init_db(app)

    from .routes import bp as api_bp

    app.register_blueprint(api_bp)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app
