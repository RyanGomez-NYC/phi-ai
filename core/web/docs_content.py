# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Server-rendered, animated architecture and data-flow diagrams for the
documentation section.

This interface runs under `script-src 'none'` (core/web/security.py), so
the diagrams cannot be drawn by a charting library. They do not need to
be: SVG with SMIL animation (<animateMotion>, <animate>) is markup, not
script, and renders animated under the strictest CSP. The generator
below mirrors the demo's d3 engine - boxes, curved links with
arrowheads, dots traveling the links, pulsing audit threads, and a
plain-English <title> tooltip on every node - as static markup.

Colors are the design system's: green = records arriving, navy = people
and the AI reading under the rules, orange = records leaving, gray =
reporting to the audit trail, red = a refusal branch.
"""

from __future__ import annotations

from html import escape

_COLORS = {
    "in": "#1d7a4f", "read": "#002D72", "out": "#FF5910",
    "audit": "#8a95a9", "refuse": "#c2440d",
}
_TIER_FILL = {"source": "#eef7f1", "ingest": "#f0f4fa", "store": "#fff",
              "gate": "#fdf9f1", "use": "#f0f4fa", "out": "#fff6f1",
              "audit": "#f7f9fc"}
_TIER_LINE = {"source": "#cfe4d8", "ingest": "#ccd6e6", "store": "#002D72",
              "gate": "#e8d5b0", "use": "#ccd6e6", "out": "#f3c1ad",
              "audit": "#e2e8f2"}

_AUDIT_DESC = ("The audit trail: an append-only, hash-chained record of every "
               "action and every refusal. Changing history breaks the chain "
               "visibly.")


def _port(node: dict, side: str) -> tuple[float, float]:
    x, y, w, h = node["x"], node["y"], node["w"], node["h"]
    if side == "r":
        return x + w, y + h / 2
    if side == "l":
        return x, y + h / 2
    if side == "b":
        return x + w / 2, y + h
    return x + w / 2, y


def _path(nodes: dict, link: dict) -> str:
    a = _port(nodes[link["from"]], link.get("fromSide", "r"))
    b = _port(nodes[link["to"]], link.get("toSide", "l"))
    if link.get("fromSide") == "b" or link.get("toSide") == "t":
        dy = max(28.0, abs(b[1] - a[1]) / 2)
        return (f"M{a[0]:.0f},{a[1]:.0f} C{a[0]:.0f},{a[1] + dy:.0f} "
                f"{b[0]:.0f},{b[1] - dy:.0f} {b[0]:.0f},{b[1]:.0f}")
    dx = max(36.0, abs(b[0] - a[0]) / 2)
    return (f"M{a[0]:.0f},{a[1]:.0f} C{a[0] + dx:.0f},{a[1]:.0f} "
            f"{b[0] - dx:.0f},{b[1]:.0f} {b[0]:.0f},{b[1]:.0f}")


def _bezier_mid(nodes: dict, link: dict) -> tuple[float, float]:
    """Point at t=0.5 of the cubic - where the label sits."""
    a = _port(nodes[link["from"]], link.get("fromSide", "r"))
    b = _port(nodes[link["to"]], link.get("toSide", "l"))
    if link.get("fromSide") == "b" or link.get("toSide") == "t":
        dy = max(28.0, abs(b[1] - a[1]) / 2)
        c1, c2 = (a[0], a[1] + dy), (b[0], b[1] - dy)
    else:
        dx = max(36.0, abs(b[0] - a[0]) / 2)
        c1, c2 = (a[0] + dx, a[1]), (b[0] - dx, b[1])
    mx = (a[0] + 3 * c1[0] + 3 * c2[0] + b[0]) / 8
    my = (a[1] + 3 * c1[1] + 3 * c2[1] + b[1]) / 8
    return mx, my


def render(spec: dict) -> str:
    """One diagram spec (nodes, links, w, h, id, alt) to an SVG string."""
    nodes = {n["id"]: n for n in spec["nodes"]}
    sid = spec["id"]
    out = [f'<svg viewBox="0 0 {spec["w"]} {spec["h"]}" role="img" '
           f'aria-label="{escape(spec.get("alt", "data flow diagram"))}" '
           f'xmlns="http://www.w3.org/2000/svg">']
    out.append("<defs>")
    for kind, color in _COLORS.items():
        out.append(
            f'<marker id="arr-{kind}-{sid}" viewBox="0 0 10 10" refX="9" '
            f'refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{color}"/></marker>')
    out.append("</defs>")

    # Links under the boxes.
    for i, link in enumerate(spec["links"]):
        kind = link["kind"]
        d = _path(nodes, link)
        dash = ' stroke-dasharray="2 4"' if kind == "audit" else (
            ' stroke-dasharray="5 4"' if kind == "refuse" else "")
        width = 1 if kind == "audit" else 1.8
        opacity = 0.55 if kind == "audit" else 0.9
        pid = f"lk-{sid}-{i}"
        out.append(
            f'<path id="{pid}" d="{d}" fill="none" stroke="{_COLORS[kind]}" '
            f'stroke-width="{width}"{dash} opacity="{opacity}" '
            f'marker-end="url(#arr-{kind}-{sid})">')
        if kind == "audit":
            # The audit threads pulse instead of streaming.
            out.append(
                f'<animate attributeName="opacity" values="0.55;0.18;0.55" '
                f'dur="3.2s" begin="{(i % 4) * 0.8:.1f}s" '
                f'repeatCount="indefinite"/>')
        out.append("</path>")
        if kind != "audit":
            # A dot travels the link - SMIL, no script.
            dur = (link.get("dur", 2600) + (i % 5) * 340) / 1000
            begin = (i % 4) * 0.6
            r = 3 if kind == "refuse" else 3.4
            out.append(
                f'<circle r="{r}" fill="{_COLORS[kind]}" opacity="0.9">'
                f'<animateMotion dur="{dur:.2f}s" begin="{begin:.1f}s" '
                f'repeatCount="indefinite" rotate="none">'
                f'<mpath href="#{pid}"/></animateMotion></circle>')
        if link.get("label"):
            mx, my = _bezier_mid(nodes, link)
            mx += link.get("labelDx", 0)
            my += link.get("labelDy", -7)
            out.append(
                f'<text x="{mx:.0f}" y="{my:.0f}" text-anchor="middle" '
                f'font-family="IBM Plex Mono, monospace" font-size="9.5" '
                f'fill="{_COLORS[kind]}">{escape(link["label"])}</text>')

    # Boxes.
    for n in spec["nodes"]:
        tier = n.get("tier", "use")
        out.append(f'<g style="cursor:help">'
                   f'<title>{escape(n.get("desc", ""))}</title>'
                   f'<rect x="{n["x"]}" y="{n["y"]}" width="{n["w"]}" '
                   f'height="{n["h"]}" rx="8" fill="{_TIER_FILL[tier]}" '
                   f'stroke="{_TIER_LINE[tier]}" '
                   f'stroke-width="{1.6 if tier == "store" else 1}"/>')
        lines = n["label"].split("\n")
        for i, line in enumerate(lines):
            y = n["y"] + n["h"] / 2 + (i - (len(lines) - 1) / 2) * 14 + 4
            family = ("IBM Plex Sans, sans-serif" if i == 0
                      else "IBM Plex Mono, monospace")
            size = 12 if i == 0 else 9.5
            weight = 600 if i == 0 else 400
            fill = "#16233d" if i == 0 else "#6a7690"
            out.append(
                f'<text x="{n["x"] + n["w"] / 2:.0f}" y="{y:.0f}" '
                f'text-anchor="middle" font-family="{family}" '
                f'font-size="{size}" font-weight="{weight}" '
                f'fill="{fill}">{escape(line)}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------
# The diagram specs - the same five pictures the demo draws.
# ---------------------------------------------------------------------------

_ARCH = {
    "id": "arch", "w": 1180, "h": 620,
    "alt": "PHI AI end-to-end architecture with animated data flows",
    "nodes": [
        {"id": "emr", "x": 20, "y": 96, "w": 150, "h": 92, "tier": "source",
         "label": "Source EMRs\nEpic · Oracle · athena…",
         "desc": "The health system's medical record systems — data planes "
                 "into the AI-native platform: records arrive from them, and "
                 "signed documentation delivers back to the chart of legal "
                 "record."},
        {"id": "bulk", "x": 236, "y": 40, "w": 150, "h": 64, "tier": "ingest",
         "label": "Bulk import\nFHIR $export",
         "desc": "Whole patient populations arrive as scheduled bulk runs, "
                 "honoring each vendor's rate limits."},
        {"id": "stream", "x": 236, "y": 140, "w": 150, "h": 64, "tier": "ingest",
         "label": "Streaming intake\nADT · labs · 835",
         "desc": "Admissions, results and remittance arrive in real time on "
                 "checkpointed feeds; gaps are surfaced, never papered over."},
        {"id": "esi", "x": 452, "y": 88, "w": 150, "h": 72, "tier": "ingest",
         "label": "Encrypt · store\n· index",
         "desc": "Every record is encrypted, stored, classified for "
                 "sensitivity, and indexed before anything can read it."},
        {"id": "store", "x": 668, "y": 56, "w": 168, "h": 88, "tier": "store",
         "label": "Data stores\nclinical · financial · BH",
         "desc": "The encrypted holdings: charts, labs, notes, imaging, "
                 "claims. Psychotherapy content lives in its own store."},
        {"id": "psych", "x": 668, "y": 168, "w": 168, "h": 56, "tier": "gate",
         "label": "Psychotherapy store\nseparate · role-locked",
         "desc": "Psychotherapy notes live apart, reachable only by the "
                 "Psychotherapy role, and never export."},
        {"id": "gates", "x": 600, "y": 292, "w": 170, "h": 76, "tier": "gate",
         "label": "Role · consent ·\npurpose gates",
         "desc": "Every read passes role permissions, sensitivity rules and "
                 "the stated purpose of use. Refusals are recorded events."},
        {"id": "rag", "x": 372, "y": 282, "w": 160, "h": 96, "tier": "use",
         "label": "PHI RAG\nretrieval-tuned LLM",
         "desc": "The AI assistant. It reads only through the same audited, "
                 "role-scoped tools people use, and cites every source."},
        {"id": "sonnet", "x": 372, "y": 420, "w": 160, "h": 56, "tier": "use",
         "label": "Your LLM\nprovider-pluggable",
         "desc": "The foundation model PHI RAG runs on - the one you bring, "
                 "under your own BAA with your AI provider. It receives "
                 "retrieved excerpts, never a database connection."},
        {"id": "people", "x": 128, "y": 282, "w": 168, "h": 96, "tier": "use",
         "label": "People & screens\nleast privilege",
         "desc": "Clinicians, HIM, analysts, psychotherapy, administrators — "
                 "each sees exactly what their role allows."},
        {"id": "sign", "x": 128, "y": 430, "w": 168, "h": 60, "tier": "gate",
         "label": "Human signature\ndrafts → signed",
         "desc": "AI output is a draft until a licensed person signs it. "
                 "Nothing enters the chart on its own."},
        {"id": "export", "x": 850, "y": 292, "w": 160, "h": 76, "tier": "out",
         "label": "Export managers\nbulk & streaming",
         "desc": "Records leave only through the export managers: "
                 "consent-gated, exclusion counts recorded, refusals shown."},
        {"id": "dest", "x": 1010, "y": 96, "w": 150, "h": 92, "tier": "out",
         "label": "Recipients\ntarget EMR · HIE ·\nregistries · payers",
         "desc": "Where allowed records go: the target EMR, health "
                 "information exchanges, registries, clearinghouses."},
        {"id": "audit", "x": 320, "y": 540, "w": 540, "h": 52, "tier": "audit",
         "label": "Audit trail — append-only hash chain", "desc": _AUDIT_DESC},
    ],
    "links": [
        {"from": "emr", "to": "bulk", "kind": "in", "label": "populations"},
        {"from": "emr", "to": "stream", "kind": "in", "label": "live events"},
        {"from": "bulk", "to": "esi", "kind": "in"},
        {"from": "stream", "to": "esi", "kind": "in"},
        {"from": "esi", "to": "store", "kind": "in",
         "label": "classified & encrypted"},
        {"from": "esi", "to": "psych", "kind": "in", "fromSide": "b",
         "toSide": "l", "dur": 3400},
        {"from": "store", "to": "gates", "kind": "read", "fromSide": "b",
         "toSide": "t"},
        # Psychotherapy content IS readable - by the psychotherapy role,
        # through the same gates, recorded as a disclosure. Drawing it as
        # a store nothing reads understated the surface, which on a
        # governance diagram is the wrong direction to be wrong in. What
        # it cannot do is leave: there is still no psych -> export edge.
        {"from": "psych", "to": "gates", "kind": "read", "fromSide": "b",
         "toSide": "r", "label": "confirmed disclosure", "dur": 4200},
        {"from": "gates", "to": "rag", "kind": "read", "label": "scoped tools"},
        {"from": "rag", "to": "people", "kind": "read",
         "label": "cited answers"},
        {"from": "rag", "to": "sonnet", "kind": "read", "fromSide": "b",
         "toSide": "t", "label": "excerpts only", "dur": 2000},
        {"from": "people", "to": "sign", "kind": "read", "fromSide": "b",
         "toSide": "t", "dur": 3000},
        {"from": "sign", "to": "emr", "kind": "out", "fromSide": "l",
         "toSide": "b", "label": "signed write-back", "labelDx": -8},
        {"from": "store", "to": "export", "kind": "out", "fromSide": "r",
         "toSide": "t", "label": "consent-gated"},
        {"from": "export", "to": "dest", "kind": "out", "fromSide": "r",
         "toSide": "b"},
        {"from": "gates", "to": "audit", "kind": "audit", "fromSide": "b",
         "toSide": "t"},
        {"from": "rag", "to": "audit", "kind": "audit", "fromSide": "b",
         "toSide": "t"},
        {"from": "export", "to": "audit", "kind": "audit", "fromSide": "b",
         "toSide": "t"},
        {"from": "esi", "to": "audit", "kind": "audit", "fromSide": "b",
         "toSide": "l"},
        {"from": "sign", "to": "audit", "kind": "audit", "fromSide": "b",
         "toSide": "l"},
    ],
}

_RAG = {
    "id": "rag", "w": 1180, "h": 300,
    "alt": "How a question becomes a cited answer",
    "nodes": [
        {"id": "user", "x": 20, "y": 60, "w": 130, "h": 70, "tier": "use",
         "label": "A person asks\nrole + purpose",
         "desc": "A signed-in profile asks a question. Their role and stated "
                 "purpose of use travel with it."},
        {"id": "rag", "x": 210, "y": 60, "w": 150, "h": 70, "tier": "use",
         "label": "PHI RAG\nplans retrieval",
         "desc": "The assistant decides which of its narrow tools can answer "
                 "the question."},
        {"id": "gate", "x": 420, "y": 60, "w": 150, "h": 70, "tier": "gate",
         "label": "Withholding gate\nrole · consent",
         "desc": "Each tool call passes the same gates a screen does. What is "
                 "withheld is counted and stated in the answer."},
        {"id": "store", "x": 630, "y": 60, "w": 140, "h": 70, "tier": "store",
         "label": "Data stores",
         "desc": "The encrypted holdings. Only the gate-approved excerpts "
                 "come back out."},
        {"id": "model", "x": 630, "y": 200, "w": 140, "h": 60, "tier": "use",
         "label": "Your LLM",
         "desc": "The foundation model you bring reads the retrieved excerpts "
                 "and writes the answer. It never touches the stores itself."},
        {"id": "ans", "x": 840, "y": 60, "w": 170, "h": 70, "tier": "use",
         "label": "Cited answer\n[source · chart]",
         "desc": "Every claim in the answer carries a citation back to the "
                 "record it came from — and says what was withheld."},
        {"id": "audit", "x": 210, "y": 210, "w": 360, "h": 50, "tier": "audit",
         "label": "Audit trail", "desc": _AUDIT_DESC},
    ],
    "links": [
        {"from": "user", "to": "rag", "kind": "read"},
        {"from": "rag", "to": "gate", "kind": "read", "label": "tool call"},
        {"from": "gate", "to": "store", "kind": "read"},
        {"from": "store", "to": "model", "kind": "read", "fromSide": "b",
         "toSide": "t", "label": "excerpts", "labelDx": 26},
        {"from": "model", "to": "ans", "kind": "read", "fromSide": "r",
         "toSide": "b", "label": "drafted answer", "labelDx": 20},
        {"from": "gate", "to": "ans", "kind": "read", "fromSide": "t",
         "toSide": "t", "label": "withheld: counted", "labelDy": -6,
         "dur": 3600},
        {"from": "rag", "to": "audit", "kind": "audit", "fromSide": "b",
         "toSide": "t"},
        {"from": "gate", "to": "audit", "kind": "audit", "fromSide": "b",
         "toSide": "t"},
    ],
}

_IMPORT = {
    "id": "imp", "w": 1180, "h": 300,
    "alt": "How records arrive in bulk",
    "nodes": [
        {"id": "emr", "x": 20, "y": 60, "w": 140, "h": 70, "tier": "source",
         "label": "Source EMR\npatient group",
         "desc": "The vendor's bulk export API, with its real limits — Epic "
                 "allows one run per group per 24 hours."},
        {"id": "kick", "x": 220, "y": 60, "w": 140, "h": 70, "tier": "ingest",
         "label": "Kickoff\n$export",
         "desc": "The run starts asynchronously against the whole patient "
                 "group."},
        {"id": "limit", "x": 220, "y": 190, "w": 140, "h": 60, "tier": "gate",
         "label": "24-hour window",
         "desc": "A second kickoff inside the vendor window is refused — and "
                 "the refusal is itself a recorded run."},
        {"id": "poll", "x": 420, "y": 60, "w": 130, "h": 70, "tier": "ingest",
         "label": "Status poll",
         "desc": "The platform polls until the vendor says the files are "
                 "ready."},
        {"id": "dl", "x": 610, "y": 60, "w": 150, "h": 70, "tier": "ingest",
         "label": "Download\nNDJSON files",
         "desc": "One file per record type. An interrupted download marks the "
                 "run failed — never partially complete."},
        {"id": "esi", "x": 830, "y": 60, "w": 160, "h": 70, "tier": "store",
         "label": "Encrypt · store\n· index",
         "desc": "Every record is encrypted, classified for sensitivity and "
                 "indexed. Only a clean run advances the watermark."},
        {"id": "audit", "x": 480, "y": 200, "w": 340, "h": 50, "tier": "audit",
         "label": "Audit trail", "desc": _AUDIT_DESC},
    ],
    "links": [
        {"from": "emr", "to": "kick", "kind": "in"},
        {"from": "kick", "to": "poll", "kind": "in"},
        {"from": "poll", "to": "dl", "kind": "in"},
        {"from": "dl", "to": "esi", "kind": "in",
         "label": "clean run → watermark"},
        {"from": "kick", "to": "limit", "kind": "refuse", "fromSide": "b",
         "toSide": "t", "label": "too soon: REFUSED", "labelDx": 6},
        {"from": "dl", "to": "audit", "kind": "audit", "fromSide": "b",
         "toSide": "t"},
        {"from": "limit", "to": "audit", "kind": "audit", "fromSide": "r",
         "toSide": "l"},
    ],
}

_EXPORT = {
    "id": "exp", "w": 1180, "h": 320,
    "alt": "How records leave, and what refuses",
    "nodes": [
        {"id": "req", "x": 20, "y": 70, "w": 150, "h": 76, "tier": "use",
         "label": "Export request\nscope · destination",
         "desc": "A delivery to the target EMR, an HIE contribution, a "
                 "registry submission, or a release-of-information "
                 "production."},
        {"id": "gate", "x": 240, "y": 70, "w": 170, "h": 76, "tier": "gate",
         "label": "Consent &\nredisclosure gate",
         "desc": "Psychotherapy notes never leave. Part 2 records refuse "
                 "redisclosure without their own consent. Other sensitive "
                 "categories are excluded per agreement."},
        {"id": "refuse", "x": 240, "y": 210, "w": 170, "h": 56, "tier": "gate",
         "label": "REFUSED\nrecorded, visible",
         "desc": "A refused export is a first-class outcome: recorded in the "
                 "run history and on the audit trail."},
        {"id": "pack", "x": 490, "y": 70, "w": 170, "h": 76, "tier": "out",
         "label": "Package\nexclusions counted",
         "desc": "What passes is packaged with the exclusion counts recorded "
                 "— withheld records are numbers in the run history, never a "
                 "silent gap."},
        {"id": "dest", "x": 740, "y": 70, "w": 160, "h": 76, "tier": "out",
         "label": "Delivery\ntarget EMR · HIE…",
         "desc": "Delivered on the vendor's write surface. A destination with "
                 "no bulk write path refuses rather than degrading."},
        {"id": "ack", "x": 980, "y": 70, "w": 130, "h": 76, "tier": "out",
         "label": "Acknowledged",
         "desc": "Streaming deliveries track acknowledged vs produced "
                 "sequence; the difference is held and accounted, never "
                 "dropped."},
        {"id": "audit", "x": 520, "y": 220, "w": 340, "h": 50, "tier": "audit",
         "label": "Audit trail", "desc": _AUDIT_DESC},
    ],
    "links": [
        {"from": "req", "to": "gate", "kind": "out"},
        {"from": "gate", "to": "pack", "kind": "out", "label": "allowed set"},
        {"from": "gate", "to": "refuse", "kind": "refuse", "fromSide": "b",
         "toSide": "t", "label": "no consent: REFUSED", "labelDx": 10},
        {"from": "pack", "to": "dest", "kind": "out"},
        {"from": "dest", "to": "ack", "kind": "out"},
        {"from": "pack", "to": "audit", "kind": "audit", "fromSide": "b",
         "toSide": "t"},
        {"from": "refuse", "to": "audit", "kind": "audit", "fromSide": "r",
         "toSide": "l"},
    ],
}

_SIGN = {
    "id": "sig", "w": 1180, "h": 320,
    "alt": "How an AI draft becomes part of the legal record",
    "nodes": [
        {"id": "enc", "x": 20, "y": 70, "w": 140, "h": 76, "tier": "source",
         "label": "Encounter\nvisit · dictation",
         "desc": "A visit happens. Ambient documentation or a summarization "
                 "request produces raw material."},
        {"id": "draft", "x": 220, "y": 70, "w": 150, "h": 76, "tier": "use",
         "label": "AI draft\ncited, labeled",
         "desc": "PHI RAG drafts the note from retrieved records. The draft "
                 "is labeled as a draft and carries its citations."},
        {"id": "queue", "x": 430, "y": 70, "w": 150, "h": 76, "tier": "gate",
         "label": "Signature queue\nthe signature rule",
         "desc": "Drafts wait here. Nothing generated can enter the record "
                 "from this queue on its own — that is the signature rule."},
        {"id": "human", "x": 640, "y": 70, "w": 160, "h": 76, "tier": "use",
         "label": "Licensed reviewer\nedits · accepts",
         "desc": "A licensed professional reads the draft against its "
                 "citations, edits it, and decides. The content becomes "
                 "theirs."},
        {"id": "rej", "x": 640, "y": 210, "w": 160, "h": 56, "tier": "gate",
         "label": "Returned\nto draft",
         "desc": "A draft the reviewer does not accept goes back or is "
                 "discarded. It never advances by timeout or default."},
        {"id": "emr", "x": 880, "y": 70, "w": 150, "h": 76, "tier": "out",
         "label": "Signed write-back\nEMR legal record",
         "desc": "Only the signed note is delivered to the EMR, on the "
                 "vendor's write surface, attributed to the signer."},
        {"id": "audit", "x": 300, "y": 220, "w": 300, "h": 50, "tier": "audit",
         "label": "Audit trail", "desc": _AUDIT_DESC},
    ],
    "links": [
        {"from": "enc", "to": "draft", "kind": "read"},
        {"from": "draft", "to": "queue", "kind": "read", "label": "draft only"},
        {"from": "queue", "to": "human", "kind": "read"},
        {"from": "human", "to": "emr", "kind": "out",
         "label": "signature.committed"},
        {"from": "human", "to": "rej", "kind": "refuse", "fromSide": "b",
         "toSide": "t", "label": "not accepted", "labelDx": 8},
        {"from": "queue", "to": "audit", "kind": "audit", "fromSide": "b",
         "toSide": "t"},
        {"from": "human", "to": "audit", "kind": "audit", "fromSide": "b",
         "toSide": "r"},
    ],
}


def architecture_svg() -> str:
    return render(_ARCH)


def dataflow_svgs() -> dict[str, str]:
    return {"rag": render(_RAG), "import": render(_IMPORT),
            "export": render(_EXPORT), "sign": render(_SIGN)}
# Made by Ryan Gomez & Co. Inc.
