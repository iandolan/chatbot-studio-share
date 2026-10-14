import logging
import pathlib
from datetime import timedelta

from flask import (Blueprint, Flask, redirect, render_template, request,
                   send_from_directory, url_for)

from . import db, security
from .config import BASE_DIR, Config

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login_page():
    if not security.admin_enabled() or security.is_admin():
        return redirect(url_for("admin.dashboard_page"))
    error = None
    if request.method == "POST":
        if security.rate_limited(f"login:{security.client_ip()}", 10, 300):
            return render_template("login.html", error="Too many attempts. Try again in five minutes."), 429
        if security.login(request.form.get("password", "")):
            destination = request.args.get("next", "")
            if not destination.startswith("/") or destination.startswith("//") or "\\" in destination:
                destination = url_for("admin.dashboard_page")
            return redirect(destination)
        error = "Wrong password"
    return render_template("login.html", error=error)


@auth_bp.get("/logout")
def logout_page():
    security.logout()
    return redirect(url_for("auth.login_page"))


def create_app():
    logging.basicConfig(level=logging.INFO)
    app = Flask(
        __name__,
        static_folder=str(BASE_DIR / "static"),
        template_folder=str(BASE_DIR / "templates"),
    )
    if Config.PRODUCTION and (not Config.ADMIN_PASSWORD or Config.SECRET_KEY == "dev-secret-change-me"):
        raise RuntimeError("Production requires ADMIN_PASSWORD and a unique SECRET_KEY")
    app.secret_key = Config.SECRET_KEY
    if Config.TRUSTED_PROXY_HOPS:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=Config.TRUSTED_PROXY_HOPS,
                               x_proto=Config.TRUSTED_PROXY_HOPS)
    app.config["SESSION_COOKIE_SECURE"] = Config.PRODUCTION or Config.PUBLIC_BASE_URL.startswith("https://")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.permanent_session_lifetime = timedelta(days=14)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["MAX_CONTENT_LENGTH"] = 21 * 1024 * 1024  # 20 MB uploads + headroom

    db.init_db()
    app.teardown_appcontext(db.close_db)

    from . import routes_admin, routes_public, scheduler
    app.register_blueprint(auth_bp)
    app.register_blueprint(routes_admin.bp)
    app.register_blueprint(routes_public.bp)
    from . import routes_operations
    app.register_blueprint(routes_operations.bp)

    @app.context_processor
    def csrf_context():
        from flask import session
        import secrets
        if session.get("admin") and not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session.get("csrf_token", "")}

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith("/api/") or security.is_admin():
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz")
    def health():
        db.query("SELECT 1")
        return {"status": "ok"}
    scheduler.start(app)

    @app.get("/widget.js")
    def widget_js():
        resp = send_from_directory(app.static_folder, "widget.js",
                                   mimetype="application/javascript")
        resp.headers["Cache-Control"] = "public, max-age=300"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @app.get("/demo")
    def demo_page():
        key = request.args.get("key", "")
        bot = db.query("SELECT name FROM bots WHERE embed_key = ?", (key,), one=True)
        return render_template("demo.html", embed_key=key,
                               bot_name=bot["name"] if bot else None)

    if Config.MOCK_LLM:
        app.logger.warning(
            "MOCK AI MODE is on (no provider API key found in .env). "
            "The app is fully usable; answers are canned echoes of training data."
        )
    if not security.admin_enabled():
        app.logger.warning(
            "ADMIN_PASSWORD is not set — the dashboard is open to anyone who can reach it. "
            "Fine locally; set it before deploying."
        )
    return app
