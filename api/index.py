"""
Vercel serverless entry point.

Vercel's Python runtime looks for a ``handler`` variable that is a WSGI
callable.  We create the Flask app exactly the same way as ``run.py`` does,
then expose it as ``handler`` so that all routes defined in the Flask app are
served through this single serverless function.

All traffic is routed here via the catch-all rewrite in ``vercel.json``.
"""
from __future__ import annotations

import os
import sys

# Ensure the project root (parent of this api/ directory) is on the Python
# path so that ``run``, ``app``, ``dealmaker_generator``, etc. can be imported.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from run import app  # noqa: E402

# Vercel's @vercel/python builder expects the WSGI callable to be named
# ``handler`` (or ``app``).  Exposing both maximises compatibility.
handler = app
