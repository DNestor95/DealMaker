"""
DealMaker v2 — entry point.

Run:
    pip install -r requirements.txt
    python run.py

Production (PaaS):
    gunicorn "app:create_app()"
"""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    port = int(os.getenv("PORT", "5050"))
    app.run(debug=debug, port=port)
