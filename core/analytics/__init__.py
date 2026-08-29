# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Population analytics over the stored record set.

Distinct from core/db/index.py, which answers "what is stored and where"
without holding clinical content, and from core/db/omop_etl.py, which
writes the analytics layer. This package only ever READS - cohort
counts, facility breakdowns and name resolution, for the assistant and
for anything else that needs to ask a question about the population
rather than about one record.

Every module here connects as a read-only role and none holds a write
grant of any kind. See core/analytics/sql_guard.py on why that, rather
than anything in Python, is what makes generated SQL safe to run.
"""
# Made by Ryan Gomez & Co. Inc.
