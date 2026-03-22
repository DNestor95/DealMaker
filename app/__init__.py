from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    from app.routes.stores import bp as stores_bp
    from app.routes.simulation import bp as simulation_bp
    from app.routes.settings import bp as settings_bp

    app.register_blueprint(stores_bp)
    app.register_blueprint(simulation_bp)
    app.register_blueprint(settings_bp)

    return app
