from flask import Flask, jsonify
from flask import Flask, Response, redirect, request, session, url_for
import os
import secrets

# Endpoints that do not require authentication
_PUBLIC_ENDPOINTS = {"auth.login", "auth.login_post", "static"}


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)

    from app.routes.auth import bp as auth_bp
    from app.routes.stores import bp as stores_bp
    from app.routes.simulation import bp as simulation_bp
    from app.routes.settings import bp as settings_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(stores_bp)
    app.register_blueprint(simulation_bp)
    app.register_blueprint(settings_bp)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})
    @app.before_request
    def require_login() -> Response | None:
        if request.endpoint in _PUBLIC_ENDPOINTS:
            return None
        if not session.get("logged_in"):
            return redirect(url_for("auth.login", next=request.path))
        return None

    return app
