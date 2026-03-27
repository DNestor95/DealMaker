"""
DealMaker v2 — entry point.

Development:
    pip install -r requirements.txt
    python run.py

Production (via gunicorn / Procfile):
    gunicorn run:app
"""
import os

from dotenv import load_dotenv

load_dotenv(override=True)

from app import create_app

app = create_app()

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    port = int(os.getenv("PORT", "5050"))
    app.run(debug=debug, host="0.0.0.0", port=port)
