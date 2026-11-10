"""Entry point for a hosted single-process server (waitress, gunicorn -w 1).

The workspace is saved whole on every edit, so this app must run as exactly ONE worker.
Two workers would race each other's saves, which the revision check would then reject.
"""
import app

application = app.app
