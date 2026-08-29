# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The assistant's knowledge base: this project's own documentation.

WHY RETRIEVAL RATHER THAN A LARGER PROMPT, AND WHY LEXICAL RATHER THAN
EMBEDDINGS. The runbooks and docs together are far too large to put in
every request, so something has to choose which parts are relevant.
Embeddings would choose better in the abstract, and would also mean a
second network dependency, a vector store to operate, and an indexing
step that must be re-run whenever a runbook changes - three new operational
surfaces for a component whose whole value is telling an operator how to
run the existing ones. A BM25-style lexical score over headings and body
text, in the standard library, needs none of that and does well on this
corpus specifically: operators search for the exact identifiers the docs
are written around (PHI_AI_DB_HOST, Object Lock, bulk export, digest
mismatch), and those match literally.

WHAT IS IN THE CORPUS, AND WHY THE DENY LIST IS SEPARATE FROM THE GLOBS.
Only committed documentation: README.md, docs/, runbooks/, the per-cloud
deploy READMEs, and the annotated .env.example and config examples. The
globs already exclude everything else, so the deny list below is
redundant - which is exactly why it is there. A glob is one edit away
from matching .env or a .pem, and the consequence of that edit would be
this project's own credentials being retrieved and sent to a model. The
deny check runs on every file the globs return, so widening a glob cannot
by itself leak a secret.

DOCUMENTS ARE DATA, NOT INSTRUCTIONS. Retrieved text is put in tool
results and the system prompt tells the model to treat it as reference
material. A deployer who writes "ignore your instructions" into their own
runbook is attacking themselves, but the framing matters for the case
that is not self-inflicted: a runbook edited by someone who should not
have been able to edit it.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from core.assistant import redact
from core.config.settings import env_var

log = logging.getLogger("phi-ai.assistant.knowledge")

# Resolved from THIS FILE, not the working directory - the same reasoning
# core/web/app.py gives for its template directory. PHI_AI_ASSISTANT_DOCS_ROOT
# overrides it for a container that mounts the docs somewhere else.
_REPO_ROOT = Path(__file__).resolve().parents[2]

CORPUS_GLOBS = (
    "README.md",
    "docs/*.md",
    "runbooks/*.md",
    "deploy/*/README.md",
    "deploy/*/README_*.md",
    ".env.example",
    "config/*.example.yaml",
)

# Never loaded, whatever the globs say. See the module docstring.
_DENY_SUBSTRINGS = (".env",)          # matched against the file NAME
_DENY_SUFFIXES = (".pem", ".key", ".tfvars", ".tfstate", ".p12", ".pfx")
_DENY_EXACT = ("smart_issuers.yaml", "retention_ruleset.yaml", "terraform.tfvars")

# .env.example is the one intentional exception to the ".env" substring
# rule: it is a committed template whose entire content is the annotated
# variable list an operator needs explained, and it contains no values.
_DENY_EXCEPTIONS = (".env.example",)

# Sections longer than this are split, so one retrieval cannot dominate
# the request. Roughly 450 tokens.
_MAX_SECTION_CHARS = 1800

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_WORD = re.compile(r"[a-z0-9_]{2,}")

# Removed before scoring only. Kept deliberately short: this corpus is
# full of terms a general stopword list would discard ("no", "not", and
# "all" all carry meaning in "no Object Lock", "not enforced", "all three
# clouds").
_STOPWORDS = frozenset(
    "the a an and or of to in is are was were be been it its this that for from with "
    "as at by on if then than so do does did how what when where which who why can "
    "could should would will i you we my our your".split()
)


@dataclass(frozen=True)
class Section:
    """One retrievable piece of one document."""

    path: str          # repo-relative, e.g. "runbooks/RUNBOOK_AWS_SETUP.md"
    heading: str       # the nearest heading, or the document title
    anchor: str        # heading trail, e.g. "Step 6a > Postgres index"
    text: str

    @property
    def citation(self) -> str:
        return f"{self.path}" + (f" > {self.anchor}" if self.anchor else "")


@dataclass(frozen=True)
class Excerpt:
    section: Section
    score: float


def _is_denied(path: Path) -> bool:
    name = path.name
    if name in _DENY_EXCEPTIONS:
        return False
    if name in _DENY_EXACT:
        return True
    if any(name.endswith(suffix) for suffix in _DENY_SUFFIXES):
        return True
    return any(fragment in name for fragment in _DENY_SUBSTRINGS)


def _split_sections(path: str, text: str) -> list[Section]:
    """Break a markdown (or annotated .env) file into heading-scoped chunks.

    Files with no headings at all - .env.example is one - fall through to
    fixed-size chunking under the document's own name, which is the right
    outcome: its comment blocks are the content, and they are already
    grouped by the variable they document.
    """
    title = path.rsplit("/", 1)[-1]
    lines = text.splitlines()

    chunks: list[tuple[str, list[str], list[str]]] = []  # (heading, trail, body)
    trail: list[str] = []
    heading = title
    body: list[str] = []

    for line in lines:
        match = _HEADING.match(line)
        if not match:
            body.append(line)
            continue
        if body:
            chunks.append((heading, list(trail), body))
        level = len(match.group(1))
        heading = match.group(2).strip()
        trail = trail[: level - 1]
        trail.append(heading)
        body = []
    if body:
        chunks.append((heading, list(trail), body))

    sections: list[Section] = []
    for chunk_heading, chunk_trail, chunk_body in chunks:
        content = "\n".join(chunk_body).strip()
        if not content:
            continue
        anchor = " > ".join(chunk_trail)
        for piece in _split_long(content):
            sections.append(
                Section(path=path, heading=chunk_heading, anchor=anchor, text=piece)
            )
    return sections


def _split_long(content: str) -> list[str]:
    if len(content) <= _MAX_SECTION_CHARS:
        return [content]
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in content.split("\n\n"):
        if size and size + len(paragraph) > _MAX_SECTION_CHARS:
            pieces.append("\n\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph) + 2
    if current:
        pieces.append("\n\n".join(current))
    return pieces


def _tokenize(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in _STOPWORDS]


class KnowledgeBase:
    """Loaded once per process; the corpus is a few hundred kilobytes."""

    def __init__(self, sections: list[Section], root: Path):
        self.root = root
        self.sections = sections
        self._documents = sorted({s.path for s in sections})

        # Document frequency per term, for IDF. Computed once.
        self._section_terms: list[dict[str, int]] = []
        document_frequency: dict[str, int] = {}
        for section in sections:
            counts: dict[str, int] = {}
            for token in _tokenize(section.text):
                counts[token] = counts.get(token, 0) + 1
            # Heading terms are counted into the same bag, weighted, so a
            # section titled "Bulk Data Export" outranks one that mentions
            # bulk export in passing.
            for token in _tokenize(f"{section.heading} {section.anchor} {section.path}"):
                counts[token] = counts.get(token, 0) + 3
            self._section_terms.append(counts)
            for token in counts:
                document_frequency[token] = document_frequency.get(token, 0) + 1

        total = max(len(sections), 1)
        self._idf = {
            token: math.log(1 + total / df) for token, df in document_frequency.items()
        }

    @property
    def documents(self) -> list[str]:
        return list(self._documents)

    def is_empty(self) -> bool:
        return not self.sections

    def search(self, query: str, limit: int = 5) -> list[Excerpt]:
        terms = _tokenize(query)
        if not terms or not self.sections:
            return []

        scored: list[Excerpt] = []
        for index, counts in enumerate(self._section_terms):
            score = 0.0
            matched = 0
            for term in terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                matched += 1
                # Saturating term frequency: the tenth mention of "bucket"
                # says little the second did not.
                score += self._idf.get(term, 0.0) * (tf / (tf + 1.5))
            if not score:
                continue
            # Favour sections matching more DISTINCT query terms over one
            # matching a single term repeatedly - the difference between a
            # section about "bulk export scheduling" and one that says
            # "export" eight times.
            score *= 1 + (matched / len(terms))
            scored.append(Excerpt(section=self.sections[index], score=score))

        scored.sort(key=lambda e: e.score, reverse=True)
        return scored[:limit]

    def read(self, path: str, section: Optional[str] = None) -> Optional[str]:
        """Full text of an indexed document, or of one section of it.

        Serves ONLY paths already in the index. A model asking for
        "../../.env" or an absolute path gets None, because the lookup is
        an exact match against the loaded document list rather than a
        filesystem operation. That is the property worth having here: no
        path traversal check can be got wrong if no path is ever joined.
        """
        if path not in self._documents:
            return None

        sections = [s for s in self.sections if s.path == path]
        if section:
            wanted = section.strip().lower()
            sections = [
                s
                for s in sections
                if wanted in s.heading.lower() or wanted in s.anchor.lower()
            ]
            if not sections:
                return None

        parts = []
        last_anchor = None
        for s in sections:
            if s.anchor != last_anchor:
                parts.append(f"## {s.anchor or s.heading}")
                last_anchor = s.anchor
            parts.append(s.text)
        return "\n\n".join(parts)


def load(root: Optional[Path] = None) -> KnowledgeBase:
    """Read the corpus off disk.

    A file that trips the corpus PHI scan is SKIPPED rather than failing
    the load: one runbook with live output pasted into it should not take
    the assistant down, but it must not be retrievable either. It is
    logged at error level so someone fixes the document.

    The docs root is read through env_var(), not os.environ.get(): a
    missed read here does not raise, it silently indexes the wrong
    directory - and in a container that mounts the docs elsewhere, that
    means an EMPTY knowledge base and an assistant answering from the
    model's own knowledge, which is the failure this whole module exists
    to avoid.
    """
    base = root or Path(env_var("ASSISTANT_DOCS_ROOT") or _REPO_ROOT)

    sections: list[Section] = []
    seen: set[Path] = set()
    for pattern in CORPUS_GLOBS:
        for path in sorted(base.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            if _is_denied(path):
                log.warning("assistant corpus: refusing to load %s", path.name)
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log.warning("assistant corpus: could not read %s: %s", path, exc)
                continue

            findings = redact.scan_corpus_text(text)
            if findings:
                log.error(
                    "assistant corpus: %s appears to contain a stored clinical "
                    "resource and has been excluded. A document under version control "
                    "should never hold one - check what was pasted into it.",
                    path.relative_to(base),
                )
                continue

            sections.extend(_split_sections(str(path.relative_to(base)), text))

    if not sections:
        log.warning(
            "the assistant's knowledge base is empty - no documentation was found "
            "under %s. The assistant will still run but can only answer from the "
            "model's own knowledge, which is exactly the failure mode it exists to "
            "avoid. In a container, check that docs/ and runbooks/ are present (see "
            "the Dockerfile) or set PHI_AI_ASSISTANT_DOCS_ROOT.",
            base,
        )
    else:
        log.info(
            "assistant knowledge base: %d sections across %d documents",
            len(sections),
            len({s.path for s in sections}),
        )
    return KnowledgeBase(sections, base)
# Made by Ryan Gomez & Co. Inc.
