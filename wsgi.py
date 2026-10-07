"""Entry point for a hosted single-process server (waitress, gunicorn -w 1).

Automatic lookup is an in-process background thread and the workbook is saved whole, so
this app must run as exactly ONE worker. Two workers would each run their own lookup
thread and race each other's saves, which the revision check would then reject.
"""
import app

app.start_automation()
application = app.app
