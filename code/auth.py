"""
Authorization from Microsoft Entra claims (Easy Auth).

App Service Authentication ("Easy Auth") validates the caller's Entra token and
injects the base64 `X-MS-CLIENT-PRINCIPAL` header. This module parses it and maps
the caller's group/role claims to entitlements:

  - base entitlement  — membership in BASE_ENTITLEMENT_GROUP_ID (required to search)
  - dataset entitlement — a restricted dataset's Entra group (grants that dataset)
  - admin              — the ADMIN_ROLE app role (required for HTTP refresh)

Enforcement is gated by AUTH_ENFORCED so the code can ship before Entra/Easy Auth
are live (flag off = no checks). All checks fail closed when enforced.
"""
import base64
import json
import os

# Claim type variants seen in the injected principal (short names and URIs).
_GROUP_TYPES = {"groups",
                "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"}
_ROLE_TYPES = {"roles",
               "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"}
_OID_TYPES = {"oid",
              "http://schemas.microsoft.com/identity/claims/objectidentifier"}


def enforced():
    return os.environ.get("AUTH_ENFORCED", "false").strip().lower() in ("1", "true", "yes")


def admin_role():
    return os.environ.get("ADMIN_ROLE", "Agent.Admin")


def base_group():
    return os.environ.get("BASE_ENTITLEMENT_GROUP_ID", "")


def get_principal(req):
    """Parse X-MS-CLIENT-PRINCIPAL into {oid, name, groups, roles}, or None."""
    raw = req.headers.get("x-ms-client-principal")
    if not raw:
        return None
    try:
        data = json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    groups, roles, oid = set(), set(), None
    for c in data.get("claims", []) or []:
        typ, val = c.get("typ"), c.get("val")
        if val is None:
            continue
        if typ in _GROUP_TYPES:
            groups.add(val)
        elif typ in _ROLE_TYPES:
            roles.add(val)
        elif typ in _OID_TYPES and oid is None:
            oid = val
    return {
        "oid": oid or req.headers.get("x-ms-client-principal-id"),
        "name": data.get("name") or req.headers.get("x-ms-client-principal-name"),
        "groups": groups,
        "roles": roles,
    }


def has_base(principal):
    """True if the caller holds the base entitlement group. Fails closed."""
    bg = base_group()
    return bool(principal) and bg != "" and bg in principal["groups"]


def is_admin(principal):
    return bool(principal) and admin_role() in principal["roles"]


def entitled_keys(principal):
    """Datasets this caller may see: all public + restricted whose group they hold."""
    from sources import public_keys, key_for_group
    keys = list(public_keys())
    if principal:
        for gid in principal["groups"]:
            k = key_for_group(gid)
            if k and k not in keys:
                keys.append(k)
    return keys
