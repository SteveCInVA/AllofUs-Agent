"""
Source registry — the single place that lists every data source the pipeline
ingests. To add a source, create a source_<name>.py that exposes a `SOURCES`
list of `Source` objects and register it here.

Dataset classification (public/restricted) and the restricted entitlement group
IDs are applied from the `DATASET_CLASSIFICATION` app-settings JSON — NOT hardcoded
in the source modules. Example value:

    {"ihcc": {"classification": "restricted", "entitlement_group_id": "<guid>"},
     "ccdi": {"classification": "restricted", "entitlement_group_id": "<guid>"}}

A dataset not listed in the config defaults to `public`. Changing the config is an
app-settings change (app restart), never an application redeploy.
"""
import json
import logging
import os

import source_allofus
import source_ihcc
import source_ccdi

# Each module contributes a list of Source objects.
_MODULES = [
    source_allofus,
    source_ihcc,
    source_ccdi,
]

ALL_SOURCES = [s for m in _MODULES for s in getattr(m, "SOURCES", [])]


def apply_classification(config=None):
    """Apply classification/entitlement config to the registered sources.

    `config` is a dict {key: {"classification": ..., "entitlement_group_id": ...}}.
    When omitted, it is read from the DATASET_CLASSIFICATION app-setting (JSON).
    Unlisted datasets keep their default (`public`, no group).
    """
    if config is None:
        raw = os.environ.get("DATASET_CLASSIFICATION")
        if not raw:
            return
        try:
            config = json.loads(raw)
        except (ValueError, TypeError):
            logging.warning("DATASET_CLASSIFICATION is not valid JSON; ignoring.")
            return
    for s in ALL_SOURCES:
        c = config.get(s.key)
        if not c:
            continue
        s.classification = c.get("classification", s.classification)
        s.entitlement_group_id = c.get("entitlement_group_id", s.entitlement_group_id)


apply_classification()  # apply from environment at import


def get_enabled_sources():
    return [s for s in ALL_SOURCES if s.enabled]


def source_keys(enabled_only=True):
    src = get_enabled_sources() if enabled_only else ALL_SOURCES
    return [s.key for s in src]


def public_keys():
    return [s.key for s in get_enabled_sources() if s.classification == "public"]


def restricted_keys():
    return [s.key for s in get_enabled_sources() if s.classification == "restricted"]


def key_for_group(group_id):
    """Return the restricted dataset key entitled by an Entra group id, or None."""
    if not group_id:
        return None
    for s in ALL_SOURCES:
        if s.classification == "restricted" and s.entitlement_group_id == group_id:
            return s.key
    return None


def get_source(key):
    for s in ALL_SOURCES:
        if s.key == key:
            return s
    return None
