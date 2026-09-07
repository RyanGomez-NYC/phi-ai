# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""Which heightened categories a chart carries, by count.

The categories are core.governance.segmentation.SensitiveCategory - the
platform's own classifier, the same one serialisation consults - so a
chart the run withholds is withheld for the reason the store would have
refused it. Nothing is classified twice two ways.

Every member of the enum is a category here, by construction - the labels
are derived from the enum, never listed by hand, and a test pins the
coverage. A hand-written list of eight left 42 CFR Part 2 out, and a Part 2
record labelled the demonstration's way (`sud_part2`) classified as clean.
"""
from __future__ import annotations

from typing import Callable, Iterable, Mapping, Optional

from core.governance.segmentation import SensitiveCategory

#: The words the screens use, per category. DERIVED from the enum: every
#: member gets a label, the overrides below only choose the wording, and a
#: member with no override still appears under its own name. Same words as
#: the demonstration where the category exists in both.
_LABEL_OVERRIDES = {
    "psychotherapy_notes": "Psychotherapy notes",
    "part2_sud": "SUD — 42 CFR Part 2",
    "reproductive_health": "Reproductive health",
    "hiv": "HIV",
    "genetic": "Genetic",
    "mental_health": "Mental health",
    "minor_consented": "Minor-consented confidential service",
    "domestic_violence": "Domestic & intimate partner violence",
    "abuse_neglect": "Abuse & neglect",
}
CATEGORY_LABELS: dict[str, str] = {
    c.value: _LABEL_OVERRIDES.get(c.value, c.value.replace("_", " ").capitalize())
    for c in SensitiveCategory
}

#: Other spellings a record may carry for the same category: the
#: demonstration and its emulators label Part 2 records `sud_part2`, and a
#: security label may use the HL7 code. Normalised to the enum's value so a
#: Part 2 record is a Part 2 record whichever way it arrived.
CATEGORY_ALIASES: dict[str, str] = {
    "sud_part2": SensitiveCategory.PART2_SUD.value,
    "42cfrpart2": SensitiveCategory.PART2_SUD.value,
    "eth": SensitiveCategory.PART2_SUD.value,          # HL7 v3 ActCode: substance abuse
    "psy": SensitiveCategory.MENTAL_HEALTH.value,      # HL7 v3 ActCode: psychiatry
    "reproductive": SensitiveCategory.REPRODUCTIVE_HEALTH.value,
    "minor_confidential": SensitiveCategory.MINOR_CONSENTED.value,
}


def normalise_category(raw: object) -> Optional[str]:
    """A label, an alias, or nothing. Case-insensitive; never invents."""
    code = str(raw or "").strip().lower()
    if not code:
        return None
    if code in CATEGORY_LABELS:
        return code
    return CATEGORY_ALIASES.get(code)


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
    cat = normalise_category(resource.get("sensitivity"))
    if cat:
        return cat
    meta = resource.get("meta") or {}
    for coding in meta.get("security") or []:
        cat = normalise_category((coding or {}).get("code"))
        if cat:
            return cat
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
