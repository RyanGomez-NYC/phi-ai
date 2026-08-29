# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Registered EMR issuers, loaded from configuration.

Kept in a FILE rather than environment variables. A deployment registers
several EMR instances, each with an issuer URL, a client id, a secret,
role mappings and a flag for whether this deployment's records came from
it. Flattening that into env vars produces names like
PHI_AI_SMART_ISSUER_3_CLIENT_SECRET and an off-by-one away from
trusting the wrong server.

The file is the allowlist that makes SMART launch safe (see
core/web/smart/launch.py), so it is treated like one: absent means no EMR
may launch, not "allow everything".
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from core.config.settings import env_var
from core.web.smart.launch import RegisteredIssuer, SMARTError

log = logging.getLogger("phi-ai.web.smart.config")

DEFAULT_PATH = "config/smart_issuers.yaml"


def load_issuers(path: str | None = None) -> list[RegisteredIssuer]:
    """Read the issuer allowlist. Returns [] when not configured.

    An empty list disables SMART launch entirely rather than allowing
    anything - the failure direction matters for an allowlist.
    """
    # env_var(), not os.environ.get(): this is the ALLOWLIST path, and a
    # miss here does not raise - it returns [] and logs "EHR launch is
    # disabled". Correct as a failure direction, but it means a
    # deployment whose variable was read under some other name would
    # silently have no SMART launch at all while believing it was
    # configured. env_var() resolves PHI_AI_SMART_ISSUERS_PATH and
    # nothing else, which is the only spelling there is.
    location = path or env_var("SMART_ISSUERS_PATH", DEFAULT_PATH) or DEFAULT_PATH
    file_path = Path(location)
    if not file_path.is_file():
        log.info("no SMART issuer file at %s - EHR launch is disabled", location)
        return []

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a hard dependency
        raise SMARTError("PyYAML is required to read the SMART issuer file") from exc

    with file_path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}

    raw_issuers = document.get("issuers") or []
    if not isinstance(raw_issuers, list):
        raise SMARTError(f"{location}: `issuers` must be a list")

    issuers: list[RegisteredIssuer] = []
    for index, entry in enumerate(raw_issuers):
        if not isinstance(entry, dict):
            raise SMARTError(f"{location}: issuer {index} is not a mapping")

        missing = [f for f in ("issuer", "vendor", "client_id") if not entry.get(f)]
        if missing:
            raise SMARTError(
                f"{location}: issuer {index} is missing required field(s): "
                f"{', '.join(missing)}"
            )

        secret = entry.get("client_secret")
        if isinstance(secret, str) and secret.startswith("env:"):
            # Secrets belong in the secret store, not in a file that gets
            # committed. The indirection is explicit so a plain string is
            # a visible choice rather than an accident.
            #
            # DELIBERATELY os.environ.get(), NOT env_var(). The name here
            # is whatever the operator wrote in their own YAML - it is
            # read literally and is not a PHI_AI_<SUFFIX> lookup. Routing
            # it through env_var() would prepend the prefix to a name
            # that already carries one, so `env:PHI_AI_SMART_EPIC_SECRET`
            # would be looked up as PHI_AI_PHI_AI_SMART_EPIC_SECRET and
            # never resolve.
            variable = secret[4:]
            secret = os.environ.get(variable)
            if not secret:
                raise SMARTError(
                    f"{location}: issuer {index} references {variable}, which is not set"
                )

        # Validated at LOAD, not at click time: a malformed deep link
        # discovered by a clinician mid-shift, while they are trying to
        # get back to a chart, is the worst moment to find out.
        chart_url = entry.get("chart_url")
        if chart_url:
            from core.web.smart.launch_back import LaunchBackError, validate_template

            try:
                chart_url = validate_template(chart_url, entry["issuer"])
            except LaunchBackError as exc:
                raise SMARTError(f"{location}: issuer {index} chart_url: {exc}") from exc

        roles = entry.get("roles") or ["viewer"]
        if isinstance(roles, str):
            roles = [r.strip() for r in roles.split(",") if r.strip()]

        # THE YAML KEY IS `record_source`, matching the RegisteredIssuer
        # field it feeds. There is no second spelling and no fallback -
        # this key, config/smart_issuers.example.yaml and the dataclass
        # field are one contract and move together.
        #
        # KNOWN GAP: this loader ignores keys it does not recognise, so a
        # misspelling here silently takes the default (True) rather than
        # failing. That matters more than a typo usually would, because
        # this flag decides whether a launching clinician is routed to a
        # patient record or to search with an explanation - defaulting
        # wrong lands them on a record view for a patient id from a
        # different EMR that cannot possibly resolve, i.e. a
        # confidently-empty chart. Rejecting unknown issuer keys outright
        # is the real fix; it changes how the whole allowlist file fails,
        # not just this field, so it is a deliberate separate change.
        record_source = bool(entry.get("record_source", True))

        issuers.append(
            RegisteredIssuer(
                issuer=entry["issuer"],
                vendor_key=str(entry["vendor"]).lower(),
                client_id=str(entry["client_id"]),
                client_secret=secret,
                label=entry.get("label", ""),
                roles=tuple(roles),
                record_source=record_source,
                embedded=bool(entry.get("embedded", False)),
                chart_url=chart_url,
                chart_label=entry.get("chart_label", entry.get("label", "")),
            )
        )

    log.info("SMART launch enabled for %d registered issuer(s)", len(issuers))
    return issuers
# Made by Ryan Gomez & Co. Inc.
