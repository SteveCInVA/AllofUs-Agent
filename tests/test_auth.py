"""Tests for auth.py — Entra principal parsing and entitlement resolution."""
import base64
import json

import azure.functions as func

import auth
import sources


def principal_header(groups=(), roles=(), oid="u1", name="User"):
    claims = [{"typ": "groups", "val": g} for g in groups]
    claims += [{"typ": "roles", "val": r} for r in roles]
    claims.append({"typ": "oid", "val": oid})
    data = {"name": name, "claims": claims}
    return base64.b64encode(json.dumps(data).encode()).decode()


def _req(headers=None):
    return func.HttpRequest(method="GET", url="/api/search",
                            headers=headers or {}, params={}, body=b"")


def test_get_principal_parses_claims():
    req = _req({"x-ms-client-principal": principal_header(
        groups=["G1", "G2"], roles=["Agent.Admin"], oid="oid-1", name="Alice")})
    p = auth.get_principal(req)
    assert p["groups"] == {"G1", "G2"}
    assert p["roles"] == {"Agent.Admin"}
    assert p["oid"] == "oid-1" and p["name"] == "Alice"


def test_get_principal_none_without_header():
    assert auth.get_principal(_req()) is None


def test_get_principal_none_when_malformed():
    assert auth.get_principal(_req({"x-ms-client-principal": "!!notbase64!!"})) is None


def test_enforced_flag(monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    assert auth.enforced()
    monkeypatch.setenv("AUTH_ENFORCED", "false")
    assert not auth.enforced()


def test_has_base_requires_configured_group(monkeypatch):
    p = {"groups": {"BASE"}, "roles": set()}
    monkeypatch.delenv("BASE_ENTITLEMENT_GROUP_ID", raising=False)
    assert auth.has_base(p) is False           # unset -> fail closed
    monkeypatch.setenv("BASE_ENTITLEMENT_GROUP_ID", "BASE")
    assert auth.has_base(p) is True
    assert auth.has_base({"groups": set(), "roles": set()}) is False
    assert auth.has_base(None) is False


def test_is_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_ROLE", "Agent.Admin")
    assert auth.is_admin({"groups": set(), "roles": {"Agent.Admin"}})
    assert not auth.is_admin({"groups": set(), "roles": {"Other"}})


def test_entitled_keys_public_plus_held_restricted():
    snap = [(s, s.classification, s.entitlement_group_id) for s in sources.ALL_SOURCES]
    try:
        sources.apply_classification({
            "ihcc": {"classification": "restricted", "entitlement_group_id": "G-IHCC"},
            "ccdi": {"classification": "restricted", "entitlement_group_id": "G-CCDI"},
        })
        base_only = auth.entitled_keys({"groups": set(), "roles": set()})
        assert set(base_only) == {"publication", "project"}     # public only

        with_ihcc = auth.entitled_keys({"groups": {"G-IHCC"}, "roles": set()})
        assert set(with_ihcc) == {"publication", "project", "ihcc"}  # + ihcc, not ccdi
        assert "ccdi" not in with_ihcc
    finally:
        for s, c, g in snap:
            s.classification, s.entitlement_group_id = c, g
