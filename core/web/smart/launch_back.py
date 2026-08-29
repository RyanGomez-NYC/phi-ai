# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Launch back: return to the EMR at the same patient or encounter.

THE COMPLEMENT TO IN-CONTEXT LAUNCH, and considerably less standardised
than it. Launching INTO an app is SMART App Launch, a specification every
target EMR implements. Launching back OUT has no equivalent: there is no
FHIR interaction meaning "open this patient's chart in your UI".

TWO MECHANISMS EXIST, and this implements the one that actually works
everywhere:

  1. A DEEP LINK to the EMR's own chart URL. Site-specific and
     operator-configured, because the URL shape differs per vendor AND per
     install. Works standalone and embedded, in every browser, with no
     JavaScript. This is what is implemented.

  2. SMART Web Messaging (HL7): an embedded app posts `ui.launchActivity`
     or `ui.done` to the host EHR frame. It is the standards-track answer
     and it is genuinely better for the embedded case - closing a panel is
     something only the host can do. It is NOT implemented here, for
     reasons worth recording rather than leaving as an omission:

       - It requires JavaScript. This interface currently ships
         `script-src 'none'`, which removes script injection as a delivery
         route outright. Trading that for a "return" button is a poor
         exchange on a PHI interface.
       - Support across the five targets is uneven, and the capability
         must be negotiated at launch. A feature that works on one EMR and
         silently does nothing on four is worse than a link that works on
         all five.

     If a deployment needs true panel-close behaviour, that is a
     deliberate decision to relax the CSP for one narrow script, and it
     should be taken explicitly.

OPEN REDIRECT IS THE RISK TO GUARD. A URL template is operator-supplied
configuration, but it renders as a link this application vouches for, so
it is validated the same way the issuer allowlist is: https only, no
credentials in the URL, and only the placeholders defined below. A
template pointing somewhere other than the EMR's own origin is permitted
- some sites front their EHR on a different hostname than the FHIR API -
but it is logged at startup so an unexpected destination is visible
rather than silent.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote, urlparse

log = logging.getLogger("phi-ai.web.smart.launch_back")

# The only substitutions permitted. A template referencing anything else
# is rejected at load rather than rendering a broken link later.
PLACEHOLDERS = frozenset({"patient", "encounter"})

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_]+)\}")


class LaunchBackError(ValueError):
    pass


def validate_template(template: str, issuer: str = "") -> str:
    """Check a chart URL template at configuration load time.

    Failing here rather than at click time is deliberate: a malformed
    deep link discovered by a clinician mid-shift, when they are trying to
    get back to a chart, is the worst possible moment to find out.
    """
    if not template or not template.strip():
        raise LaunchBackError("chart_url is empty")

    candidate = template.strip()
    unknown = {name for name in _PLACEHOLDER_RE.findall(candidate)} - PLACEHOLDERS
    if unknown:
        raise LaunchBackError(
            f"chart_url uses unknown placeholder(s): {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(sorted(PLACEHOLDERS))}."
        )

    # Parse with placeholders removed - braces are not valid URL syntax
    # and would otherwise trip the parser on an otherwise-fine template.
    parsed = urlparse(_PLACEHOLDER_RE.sub("x", candidate))
    if parsed.scheme != "https":
        raise LaunchBackError(
            f"chart_url must be https, got {parsed.scheme or 'no scheme'!r}. A link this "
            "application renders carries a patient identifier to the destination."
        )
    if not parsed.netloc:
        raise LaunchBackError("chart_url has no host")
    if "@" in parsed.netloc:
        raise LaunchBackError(
            "chart_url must not embed credentials - they would be visible to anyone "
            "reading the page or the browser history."
        )

    if issuer:
        issuer_host = urlparse(issuer).netloc.lower()
        if parsed.netloc.lower() != issuer_host:
            # Legitimate - many sites front the EHR UI on a different
            # hostname than the FHIR API - but worth seeing.
            log.info(
                "chart_url host %s differs from the FHIR issuer host %s; clinicians will "
                "be sent to the former",
                parsed.netloc, issuer_host,
            )
    return candidate


def build_chart_url(
    template: str,
    patient_id: Optional[str],
    encounter_id: Optional[str] = None,
) -> Optional[str]:
    """Render the deep link, or None when the template cannot be satisfied.

    Returns None rather than a partially-substituted URL: a chart link
    with a literal `{encounter}` in it either 404s or, worse, opens the
    wrong thing. No link is better than a wrong one.

    Values are percent-encoded. Patient and encounter ids are opaque and
    EMR-assigned, so this is belt-and-braces rather than a live concern -
    but an id is untrusted input from the moment it leaves the EMR, and
    building a URL by concatenation is how injection happens.
    """
    if not template:
        return None

    needed = set(_PLACEHOLDER_RE.findall(template))
    values = {"patient": patient_id, "encounter": encounter_id}

    for name in needed:
        if not values.get(name):
            log.debug("cannot build chart url: no %s in this context", name)
            return None

    rendered = template
    for name in needed:
        rendered = rendered.replace("{" + name + "}", quote(str(values[name]), safe=""))
    return rendered
# Made by Ryan Gomez & Co. Inc.
