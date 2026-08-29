FROM python:3.12-slim

# Run as non-root; PHI-handling services shouldn't run as root even in a
# container, on general defense-in-depth principle.
RUN useradd --create-home --shell /bin/bash phiai

# Native dependencies for OCR document ingestion (core/ocr/). pytesseract
# and pdf2image are wrappers - without these binaries they import cleanly
# and then fail at first use.
#   tesseract-ocr      the OCR engine itself
#   tesseract-ocr-eng  English language data; add more packs per language
#   poppler-utils      pdf2image shells out to it to rasterise PDF pages
# Installed before the pip layer so a requirements.txt change does not
# re-run this apt layer on every rebuild.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        tesseract-ocr-eng \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The LOCK, not requirements.txt: the lock's own header says a
# reproducible/production install uses the pinned, hash-checked set the
# tests ran against - and this image is the production install. A plain
# requirements.txt install resolves to whatever is newest on build day,
# so two builds drift apart and an unexpected release lands with nobody
# choosing it.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY core/ core/
COPY install/ install/
COPY scripts/ scripts/

# The optional assistant answers from this project's own documentation
# (core/assistant/knowledge.py), so the documentation has to exist inside
# the image - without it the assistant runs but falls back on the model's
# general knowledge, which is the failure mode it exists to prevent.
# These are committed, non-secret files, unlike the deployer's own
# config/ and .env, which stay out of the image deliberately (see
# docker-compose.yml). Copied last so editing a runbook rebuilds one
# small layer rather than reinstalling dependencies.
COPY README.md .env.example ./
COPY docs/ docs/
COPY runbooks/ runbooks/
COPY deploy/aws/README.md deploy/aws/
COPY deploy/gcp/README.md deploy/gcp/
COPY deploy/azure/README.md deploy/azure/

RUN mkdir -p /app/restore-output && chown -R phiai:phiai /app

USER phiai

CMD ["python", "-m", "core.fhir.scheduler"]
# Made by Ryan Gomez & Co. Inc.
