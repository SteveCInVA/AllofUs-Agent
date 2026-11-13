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

### Search infrastructure (PowerShell)

With Pester 5 installed, run from the repository root:

```powershell
Import-Module Pester -MinimumVersion 5.0
Invoke-Pester -Path .\tests\deploy_azure_infrastructure.Tests.ps1 -Output Detailed
```

These offline tests execute only the Search provisioning section with a fake
Azure CLI. They cover commercial/government endpoint configuration, private
networking, temporary public access with API keys disabled, the post-deployment
disable warning/command, managed-identity roles, app settings, JSON index creation/read-back,
deployer user/service-principal roles, bounded retries, and stopping on
provisioning failures. They do not create Azure resources or verify live
connectivity.

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
| `test_function_app.py` | HTTP search/refresh contracts, entitlement forwarding, Search-backed health and failure responses |
| `test_storage.py` | schema mapping, stable keys, size/count batching, source synchronization, security filters, real SDK HTTP serialization against an offline transport |
| `test_smoke.py` | legacy offline BM25 reference pipeline + opt-in live source smoke |

`fixtures/` holds tiny trimmed raw payloads per source so normalizer tests are
deterministic and offline.

The Search smoke test in `test_storage.py` runs actual normalizers, refresh,
upload/reimport, query, and health counts through the Azure Search SDK with an
in-memory HTTP transport. No Azure service or credentials are required. Separate
live deployment validation is still needed for RBAC, private networking, service
limits, eventual consistency, and government-cloud behavior.
