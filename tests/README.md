# Tests

Unit, component, and smoke tests for the Azure Function code (`../code`). This is
the **retrieval / code layer** (deterministic, mocked I/O) — separate from the
Copilot Studio agent-evaluation set.

## Install

```bash
pip install -r ../code/requirements.txt
pip install -r ../requirements-dev.txt
```

## Run

```bash
# Offline suite (default; no network) with coverage
pytest --cov=code --cov-report=term-missing

# Enforce the 85% coverage gate (as CI does)
pytest -m "not live" --cov=code --cov-fail-under=85

# Offline end-to-end smoke only
pytest -m smoke

# Live smoke (opt-in; hits real endpoints). CCDI is also 'slow' (~4 min).
pytest -m live                 # all live sources
pytest -m live -k ihcc         # a single reliable source
pytest -m "live and not slow"  # skip the slow CCDI federation call
```

## Layout

| File | Covers |
|------|--------|
| `test_search_core.py` | text helpers, artifact build, filter resolve, ranked search |
| `test_source_allofus.py` | publication + project normalizers |
| `test_source_ihcc.py` | IHCC cohort normalizer + helpers |
| `test_source_ccdi.py` | CCDI flatten/normalize + `fetch_ccdi` assembly |
| `test_sources_registry.py` | registry + cross-source required-keys contract |
| `test_feeds.py` | `fetch` retry logic + `load_source_resilient` fallbacks |
| `test_refresh_job.py` | per-source resilience, carry-forward, degraded status |
| `test_function_app.py` | HTTP handlers + three-layer cache engine |
| `test_storage.py` | connection/URL selection + blob ops (mocked) |
| `test_smoke.py` | offline end-to-end + opt-in live smoke |

`fixtures/` holds tiny trimmed raw payloads per source so normalizer tests are
deterministic and offline.
