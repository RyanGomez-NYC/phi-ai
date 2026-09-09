# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The one Jinja environment, so there is exactly one.

EXTRACTED FROM core/web/app.py when create_app was broken into routers.
app.py could not stay the owner of this: the routers it now registers
would have had to import it back, and app.py imports them - a cycle. The
alternative, each router building its own Jinja2Templates over the same
directory, is worse than a cycle: two environments over one template
directory means a filter or global registered on one is silently absent
from the other, and the symptom is a single screen rendering differently
from its neighbours.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

TEMPLATES = Jinja2Templates(directory=str(TEMPLATE_DIR))
# Made by Ryan Gomez & Co. Inc.
