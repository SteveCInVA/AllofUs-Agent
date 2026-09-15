"""
Source registry — the single place that lists every data source the pipeline
ingests. To add a source, create a source_<name>.py that exposes a `SOURCES`
list of `Source` objects and register it here.
"""
import source_allofus
import source_ihcc

# Each module contributes a list of Source objects.
_MODULES = [
    source_allofus,
    source_ihcc,
    # source_ccdi,   # added in the CCDI step
]

ALL_SOURCES = [s for m in _MODULES for s in getattr(m, "SOURCES", [])]


def get_enabled_sources():
    return [s for s in ALL_SOURCES if s.enabled]


def source_keys(enabled_only=True):
    src = get_enabled_sources() if enabled_only else ALL_SOURCES
    return [s.key for s in src]


def get_source(key):
    for s in ALL_SOURCES:
        if s.key == key:
            return s
    return None
