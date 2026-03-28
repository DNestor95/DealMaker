from flask import Flask, jsonify
import os
import secrets


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)

    from app.routes.stores import bp as stores_bp, reconcile_store_records
    from app.routes.simulation import bp as simulation_bp
    from app.routes.settings import bp as settings_bp

    app.register_blueprint(stores_bp)
    app.register_blueprint(simulation_bp)
    app.register_blueprint(settings_bp)

    reconcile_store_records()

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    return app
