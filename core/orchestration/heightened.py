# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""Which heightened categories a chart carries, by count.

The categories are core.governance.segmentation.SensitiveCategory - the
platform's own classifier, the same one serialisation consults - so a
chart the run withholds is withheld for the reason the store would have
refused it. Nothing is classified twice two ways.

Known gap: the platform's enum has no 42 CFR Part 2 (substance use disorder) category.
The demo classifies one. Until segmentation.py grows it, a Part 2 record
here is whatever the classifier already calls it, and this module does not
invent a category the classifier cannot produce.
"""
from __future__ import annotations

from typing import Callable, Iterable, Mapping, Optional

from core.governance.segmentation import SensitiveCategory

#: The words the screens use, per category. Same words as the demo where the
#: category exists in both.
CATEGORY_LABELS: dict[str, str] = {
    SensitiveCategory.PSYCHOTHERAPY_NOTES.value: "Psychotherapy notes",
    SensitiveCategory.REPRODUCTIVE_HEALTH.value: "Reproductive health",
    SensitiveCategory.HIV.value: "HIV",
    SensitiveCategory.GENETIC.value: "Genetic",
    SensitiveCategory.MENTAL_HEALTH.value: "Mental health",
    SensitiveCategory.MINOR_CONSENTED.value: "Minor-consented confidential service",
    SensitiveCategory.DOMESTIC_VIOLENCE.value: "Domestic & intimate partner violence",
    SensitiveCategory.ABUSE_NEGLECT.value: "Abuse & neglect",
}


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category.replace("_", " "))


#: A classifier answers, for one FHIR resource, the category it carries or
#: None. The routes hand in the platform's segmentation.classify bound to
#: its value sets; tests hand in a lambda. The engine does not care which.
Classifier = Callable[[Mapping], Optional[str]]


def classify_by_labels(resource: Mapping) -> Optional[str]:
    """The fallback classifier: a resource that already carries a sensitivity
    label - `meta.security` codes, or a plain `sensitivity` field as the
    emulators emit - names its own category. Anything else is clean."""
    plain = resource.get("sensitivity")
    if isinstance(plain, str) and plain in CATEGORY_LABELS:
        return plain
    meta = resource.get("meta") or {}
    for coding in meta.get("security") or []:
        code = str((coding or {}).get("code", "")).lower()
        if code in CATEGORY_LABELS:
            return code
    return None


def categories_for_chart(resources: Iterable[Mapping],
                         classify: Classifier = classify_by_labels) -> dict[str, int]:
    """category -> how many of this chart's records carry it. Empty for a
    clean chart. The Patient resource itself is never a heightened record."""
    held: dict[str, int] = {}
    for res in resources:
        if not isinstance(res, Mapping) or res.get("resourceType") == "Patient":
            continue
        cat = classify(res)
        if cat:
            held[cat] = held.get(cat, 0) + 1
    return held


def withheld_prose(withheld: Mapping[str, int], sep: str = ", ") -> str:
    """'3 × Mental health, 1 × HIV' - the categories a role may see, named."""
    return sep.join(f"{int(n)} × {category_label(str(c))}" for c, n in withheld.items())
