"""
Shared pytest fixtures + path setup.

The Function modules live in ../code and import each other by top-level name
(e.g. `import storage`), so we put that directory on sys.path for the tests.
"""
import json
import os
import sys

import pytest

CODE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "code")
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def publications_raw():
    return load_fixture("publications.json")


@pytest.fixture
def projects_raw():
    return load_fixture("projects.json")


@pytest.fixture
def ihcc_raw():
    return load_fixture("ihcc_cohorts.json")


@pytest.fixture
def ccdi_payload():
    return load_fixture("ccdi_payload.json")


@pytest.fixture
def sample_docs():
    """A tiny normalized corpus (three sources) for search/artifact tests."""
    return [
        {"id": "a1", "source": "publication", "record_type": "Publication",
         "title": "Air pollution and childhood asthma", "url": "http://a1",
         "snippet": "asthma", "_body": "air pollution childhood asthma children lungs"},
        {"id": "b1", "source": "project", "record_type": "Project",
         "title": "Maternal hypertension", "url": "http://b1",
         "snippet": "bp", "_body": "maternal pregnancy hypertension blood pressure"},
        {"id": "c1", "source": "ihcc", "record_type": "Cohort",
         "title": "Canada cohort", "url": "http://c1",
         "snippet": "cohort", "_body": "canada cohort genomic biobank asthma"},
    ]
