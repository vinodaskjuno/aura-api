"""Directory sign-in: bind, group resolution, and mapping to permissions.

Uses ldap3's MOCK_SYNC strategy, which serves a real in-memory directory — so the
search filters, the bind and the entry shapes are exercised rather than stubbed.
The escaping and fallback tests matter most: an unescaped filter is an
authentication bypass, and missing nested groups locks out users who are genuinely
entitled.
"""
from __future__ import annotations

import pytest

from src.services import auth_config, ldap_auth
from src.services.auth_config import AuthConfig, Mapping

BASE = "dc=aura,dc=local"
USERS = f"ou=users,{BASE}"
GROUPS = f"ou=groups,{BASE}"
PRIYA = f"cn=priya,{USERS}"


class FakeSettings:
    ldap_uri = "ldap://fake"
    ldap_base_dn = BASE
    ldap_bind_dn = "cn=svc,dc=aura,dc=local"
    ldap_bind_password = "svc-pw"
    ldap_user_filter = "(cn={username})"
    ldap_allow_insecure = True          # the mock server has no TLS


@pytest.fixture
def directory(monkeypatch):
    """A mock LDAP server with one user in two groups."""
    from ldap3 import Connection, MOCK_SYNC, Server

    server = Server("fake")

    def make_connection(srv, user=None, password=None, auto_bind=False, **kw):
        conn = Connection(server, user=user, password=password,
                          client_strategy=MOCK_SYNC)
        conn.strategy.add_entry("cn=svc,dc=aura,dc=local",
                                {"objectClass": "person", "userPassword": "svc-pw"})
        conn.strategy.add_entry(PRIYA, {
            "objectClass": "person",
            "userPassword": "correct-horse",
            "cn": "priya",
            "displayName": "Priya S",
            "mail": "priya@aura.local",
            "memberOf": [f"cn=AURA-Dev,{GROUPS}", f"cn=AURA-QA,{GROUPS}"],
        })
        conn.strategy.add_entry(f"cn=nomad,{USERS}", {
            "objectClass": "person", "userPassword": "pw", "cn": "nomad",
            "displayName": "No Groups", "memberOf": [],
        })
        if auto_bind and not conn.bind():
            raise RuntimeError("bind failed")
        return conn

    import ldap3
    monkeypatch.setattr(ldap3, "Connection", make_connection)
    monkeypatch.setattr(ldap_auth, "_server", lambda s: server)
    return server


# ── Injection and credential handling ────────────────────────────────────────

def test_filter_injection_is_escaped():
    """`*)(objectClass=*` in a filter is an authentication bypass."""
    escaped = ldap_auth._escape("*)(objectClass=*")
    assert "*" not in escaped and "(" not in escaped and ")" not in escaped
    assert escaped == r"\2a\29\28objectClass=\2a"


def test_an_injection_username_finds_nobody(directory):
    assert ldap_auth.authenticate("*)(objectClass=*", "anything", FakeSettings) is None


def test_wrong_password_is_refused(directory):
    assert ldap_auth.authenticate("priya", "wrong", FakeSettings) is None


def test_correct_password_succeeds(directory):
    user = ldap_auth.authenticate("priya", "correct-horse", FakeSettings)
    assert user is not None
    assert user.username == "priya"
    assert user.email == "priya@aura.local"
    assert user.user_id.startswith("ldap:")


def test_an_empty_password_is_refused_without_touching_the_server(directory):
    """An empty password is an ANONYMOUS bind, which succeeds against many
    directories — that would be a total bypass."""
    assert ldap_auth.authenticate("priya", "", FakeSettings) is None


def test_an_unknown_user_returns_none(directory):
    assert ldap_auth.authenticate("ghost", "pw", FakeSettings) is None


# ── Transport ────────────────────────────────────────────────────────────────

def test_plaintext_is_refused_unless_explicitly_allowed():
    class Insecure(FakeSettings):
        ldap_uri = "ldap://dc.corp"
        ldap_allow_insecure = False

    with pytest.raises(ldap_auth.LdapError) as exc:
        ldap_auth._server(Insecure)
    assert "clear text" in str(exc.value)


def test_ldaps_needs_no_waiver():
    class Secure(FakeSettings):
        ldap_uri = "ldaps://dc.corp:636"
        ldap_allow_insecure = False

    ldap_auth._server(Secure)          # must not raise


# ── Group resolution ─────────────────────────────────────────────────────────

def test_groups_come_back_as_names_not_dns(directory):
    user = ldap_auth.authenticate("priya", "correct-horse", FakeSettings)
    assert user.groups == ["AURA-Dev", "AURA-QA"]


def test_cn_extraction():
    assert ldap_auth.cn_of("CN=AURA-Dev,OU=Groups,DC=aura,DC=local") == "AURA-Dev"
    assert ldap_auth.cn_of("cn=AURA-Ops,ou=g,dc=x") == "AURA-Ops"
    assert ldap_auth.cn_of("AURA-Flat") == "AURA-Flat"


def test_memberof_is_the_fallback_when_nested_search_is_unsupported(directory, monkeypatch):
    """OpenLDAP has no LDAP_MATCHING_RULE_IN_CHAIN. Treating that as 'no groups'
    would lock out every user on a non-AD directory."""
    monkeypatch.setattr(ldap_auth, "_nested_groups", lambda *a: None)
    user = ldap_auth.authenticate("priya", "correct-horse", FakeSettings)
    assert user.groups == ["AURA-Dev", "AURA-QA"]


def test_a_directory_outage_raises_rather_than_denying(monkeypatch):
    """The caller must distinguish 'AD is down' (fall back to break-glass) from
    'wrong password' (never fall back)."""
    def boom(settings, server=None):
        raise ldap_auth.LdapError("connection refused")

    monkeypatch.setattr(ldap_auth, "_service_connection", boom)
    monkeypatch.setattr(ldap_auth, "_server", lambda s: object())
    with pytest.raises(ldap_auth.LdapError):
        ldap_auth.authenticate("priya", "pw", FakeSettings)


# ── Mapping groups to permissions ────────────────────────────────────────────

def cfg(*mappings) -> AuthConfig:
    return AuthConfig(enabled=True, mappings=list(mappings))


def test_a_single_group_grants_its_role_permissions():
    r = auth_config.resolve(["AURA-Dev"], cfg(Mapping("AURA-Dev", role_id="user_dev")))
    assert "dev_workspace" in r["permissions"]
    assert r["roleId"] == "user_dev"


def test_several_groups_give_the_union():
    """Anything other than a union would make access order-dependent."""
    r = auth_config.resolve(
        ["AURA-Dev", "AURA-Ops"],
        cfg(Mapping("AURA-Dev", role_id="user_dev", priority=50),
            Mapping("AURA-Ops", role_id="user_ops", priority=50)))
    assert "dev_workspace" in r["permissions"]      # from user_dev
    assert "observability" in r["permissions"]      # from user_ops
    assert set(r["matched"]) == {"AURA-Dev", "AURA-Ops"}


def test_the_highest_priority_group_names_the_role():
    r = auth_config.resolve(
        ["AURA-Dev", "AURA-Admins"],
        cfg(Mapping("AURA-Dev", role_id="user_dev", priority=50),
            Mapping("AURA-Admins", role_id="admin", priority=100)))
    assert r["roleId"] == "admin"


def test_a_mapping_can_grant_bare_permissions_without_a_role():
    """So one group can be given an extra screen without inventing a role."""
    r = auth_config.resolve(["AURA-QA-Lead"],
                            cfg(Mapping("AURA-QA-Lead", permissions=["qa_workspace", "logs"])))
    assert r["permissions"] == ["qa_workspace", "logs"]


def test_group_matching_is_case_insensitive():
    """An administrator should not have to match the directory's casing exactly."""
    r = auth_config.resolve(["aura-dev"], cfg(Mapping("AURA-Dev", role_id="user_dev")))
    assert r["matched"] == ["AURA-Dev"]


def test_unmapped_groups_grant_nothing():
    r = auth_config.resolve(["Domain Users", "Finance"],
                            cfg(Mapping("AURA-Dev", role_id="user_dev")))
    assert r["permissions"] == [] and r["matched"] == []


def test_no_groups_at_all_grants_nothing():
    assert auth_config.resolve([], cfg(Mapping("AURA-Dev", role_id="user_dev")))["permissions"] == []


# ── The dispatcher: local vs directory, and break-glass ──────────────────────
#
# These decide who can sign in when things go wrong, which is the part worth being
# paranoid about.

@pytest.fixture
def local_users(monkeypatch):
    users = {
        "admin": {"userId": "u-admin", "username": "admin", "roleId": "super_admin",
                  "status": "active", "passwordHash": "H", "breakGlass": True},
        "alice": {"userId": "u-alice", "username": "alice", "roleId": "user_dev",
                  "status": "active", "passwordHash": "H"},
    }
    from src.services import auth_service
    monkeypatch.setattr(auth_service, "get_user_by_username", lambda n: users.get(n))
    monkeypatch.setattr(auth_service, "_verify_password", lambda pw, h: pw == "good")
    monkeypatch.setattr(auth_service, "update_item", lambda *a, **k: None)
    monkeypatch.setattr(auth_service, "get_role",
                        lambda rid: {"permissions": auth_service.ROLE_PERMISSIONS.get(rid, [])})
    return users


def _ldap_off(monkeypatch):
    monkeypatch.setattr(auth_config, "get_config", lambda refresh=False: AuthConfig(enabled=False))


def _ldap_on(monkeypatch, *mappings):
    monkeypatch.setattr(auth_config, "get_config",
                        lambda refresh=False: cfg(*mappings))


def test_local_login_is_untouched_when_the_directory_is_off(local_users, monkeypatch):
    from src.services import auth_service
    _ldap_off(monkeypatch)
    user = auth_service.authenticate("alice", "good")
    assert user["userId"] == "u-alice"
    assert "dev_workspace" in user["permissions"]


def test_a_directory_user_gets_group_derived_permissions(local_users, monkeypatch):
    from src.services import auth_service
    _ldap_on(monkeypatch, Mapping("AURA-Ops", role_id="user_ops", priority=10))
    monkeypatch.setattr(ldap_auth, "authenticate",
                        lambda u, p, s=None: ldap_auth.LdapUser(
                            username="priya", dn=PRIYA, object_guid="guid-1",
                            groups=["AURA-Ops", "Domain Users"]))
    user = auth_service.authenticate("priya", "pw")
    assert user["userId"] == "ldap:guid-1"
    assert user["role"] == "user_ops"
    assert "observability" in user["permissions"]
    assert "dev_workspace" not in user["permissions"]


def test_a_directory_user_in_no_mapped_group_is_refused_with_the_group_names(
        local_users, monkeypatch):
    """A bare 'invalid credentials' would send them to reset a working password."""
    from src.services import auth_service
    _ldap_on(monkeypatch, Mapping("AURA-Dev", role_id="user_dev"))
    monkeypatch.setattr(ldap_auth, "authenticate",
                        lambda u, p, s=None: ldap_auth.LdapUser(
                            username="nomad", dn="cn=nomad", groups=["Domain Users"]))
    with pytest.raises(auth_service.DirectoryRefused) as exc:
        auth_service.authenticate("nomad", "pw")
    assert "AURA-Dev" in str(exc.value)


def test_a_wrong_directory_password_does_not_fall_back_to_local(local_users, monkeypatch):
    """Otherwise a stale local password outlives the directory account it mirrors."""
    from src.services import auth_service
    _ldap_on(monkeypatch, Mapping("AURA-Dev", role_id="user_dev"))
    monkeypatch.setattr(ldap_auth, "authenticate", lambda u, p, s=None: None)
    assert auth_service.authenticate("alice", "good") is None


def test_a_directory_outage_admits_only_break_glass(local_users, monkeypatch):
    from src.services import auth_service
    _ldap_on(monkeypatch, Mapping("AURA-Dev", role_id="user_dev"))

    def down(u, p, s=None):
        raise ldap_auth.LdapError("connection refused")

    monkeypatch.setattr(ldap_auth, "authenticate", down)

    # The marked account gets in — without it, a wrong bind DN would lock everyone
    # out of the very screen where the directory is configured.
    admin = auth_service.authenticate("admin", "good")
    assert admin is not None and admin["role"] == "super_admin"

    # An ordinary local account must NOT become usable just because AD is down.
    assert auth_service.authenticate("alice", "good") is None


# ── Group discovery across directory flavours ────────────────────────────────

def test_reverse_search_covers_the_three_membership_schemas():
    """Stock OpenLDAP records membership on the GROUP and ships no memberOf overlay,
    so this path is what makes non-AD directories work at all. Verified against a
    real OpenLDAP 1.5.0, where it is the only strategy that returns anything."""
    captured = {}

    class Conn:
        entries = []
        def search(self, search_base, search_filter, attributes=None):
            captured["filter"] = search_filter
            return True

    ldap_auth._reverse_group_search(Conn(), FakeSettings,
                                    "uid=priya,ou=users,dc=aura,dc=local", "priya")
    f = captured["filter"]
    assert "(member=" in f            # groupOfNames
    assert "(uniqueMember=" in f      # groupOfUniqueNames
    assert "(memberUid=" in f         # posixGroup


def test_group_discovery_prefers_the_transitive_answer(monkeypatch):
    monkeypatch.setattr(ldap_auth, "_nested_groups", lambda *a: ["AURA-Nested"])
    groups, source = ldap_auth.resolve_groups(
        object(), FakeSettings, "cn=x", {"memberOf": [f"cn=AURA-Flat,{GROUPS}"]})
    assert groups == ["AURA-Nested"] and "matching rule" in source


def test_group_discovery_falls_through_to_member_of(monkeypatch):
    monkeypatch.setattr(ldap_auth, "_nested_groups", lambda *a: None)
    groups, source = ldap_auth.resolve_groups(
        object(), FakeSettings, "cn=x", {"memberOf": [f"cn=AURA-Flat,{GROUPS}"]})
    assert groups == ["AURA-Flat"] and source == "memberOf"


def test_group_discovery_falls_through_to_reverse_search(monkeypatch):
    monkeypatch.setattr(ldap_auth, "_nested_groups", lambda *a: None)
    monkeypatch.setattr(ldap_auth, "_reverse_group_search", lambda *a: ["AURA-Reverse"])
    groups, source = ldap_auth.resolve_groups(object(), FakeSettings, "cn=x", {})
    assert groups == ["AURA-Reverse"] and source == "reverse member search"


def test_the_reverse_search_escapes_the_user_dn():
    """The DN comes from the directory, but it still reaches a filter."""
    captured = {}

    class Conn:
        entries = []
        def search(self, search_base, search_filter, attributes=None):
            captured["filter"] = search_filter
            return True

    ldap_auth._reverse_group_search(Conn(), FakeSettings, "cn=a*b)(c", "")
    assert "*" not in captured["filter"].replace("(|", "")


# ── /auth/me tells the UI who is in charge of access ──────────────────────────

def _me(monkeypatch, enabled):
    """Call GET /auth/me with the directory switch in a known state."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.routers import auth as auth_router
    from src.services import auth_config

    monkeypatch.setattr(auth_config, "get_config",
                        lambda: auth_config.AuthConfig(enabled=enabled, mappings=[]))

    app = FastAPI()
    app.include_router(auth_router.router)   # the router carries its own /auth prefix
    app.dependency_overrides[auth_router.get_current_user] = lambda: {
        "userId": "u1", "username": "admin", "role": "admin", "permissions": ["dashboard"]}
    return TestClient(app).get("/auth/me").json()


def test_me_reports_directory_managed(monkeypatch):
    """User and Role Management read this to explain their scope; they cannot read
    the LDAP config endpoint, which needs user_management they may not hold."""
    assert _me(monkeypatch, True)["directoryManaged"] is True
    assert _me(monkeypatch, False)["directoryManaged"] is False


def test_me_survives_an_unreadable_config(monkeypatch):
    """A DynamoDB hiccup must not break the identity call the whole UI depends on."""
    from src.services import auth_config
    monkeypatch.setattr(auth_config, "get_config",
                        lambda: (_ for _ in ()).throw(RuntimeError("dynamo down")))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.routers import auth as auth_router

    app = FastAPI()
    app.include_router(auth_router.router)   # the router carries its own /auth prefix
    app.dependency_overrides[auth_router.get_current_user] = lambda: {"username": "admin"}
    body = TestClient(app).get("/auth/me").json()
    assert body["directoryManaged"] is False and body["username"] == "admin"


def test_placeholder_bind_password_reports_as_unset(monkeypatch):
    """Terraform creates the secret holding REPLACE_ME and never sets its value.

    Reporting that as "set" would send an administrator hunting a connection fault
    that is really an unstored password — the single most likely first-run mistake.
    """
    from src.routers import auth as auth_router
    from src.services import auth_config
    from src import config_settings

    monkeypatch.setattr(auth_config, "get_config",
                        lambda refresh=False: auth_config.AuthConfig())

    # Called directly: require_permission() returns a NEW callable each time, so it
    # cannot be matched by dependency_overrides, and the permission check is not
    # what is under test here.
    for stored, expected in (("REPLACE_ME", False), ("", False), ("a-real-pw", True)):
        class S(FakeSettings):
            ldap_bind_password = stored
        monkeypatch.setattr(config_settings, "get_settings", lambda S=S: S)
        body = auth_router.get_ldap_config.__wrapped__() if hasattr(
            auth_router.get_ldap_config, "__wrapped__") else auth_router.get_ldap_config({})
        assert body["connection"]["bindPasswordSet"] is expected, stored


# ── Organization roles ───────────────────────────────────────────────────────
#
# The tier between a directory group and a permission. Several AD groups mean the
# same business function; the org role is where that is said once.

def org_cfg(roles, mappings, enabled=True) -> AuthConfig:
    return AuthConfig(enabled=enabled, org_roles=list(roles), mappings=list(mappings))


def test_two_groups_on_one_org_role_grant_the_same_thing():
    """The reason the tier exists — say it once, not once per group."""
    engineering = auth_config.OrgRole(
        id="engineering", label="Engineering",
        permissions=["dashboard", "dev_workspace"], priority=50)
    config = org_cfg(
        [engineering],
        [auth_config.Mapping("AURA-Dev", org_role_id="engineering"),
         auth_config.Mapping("platform-eng", org_role_id="engineering")])

    a = auth_config.resolve(["AURA-Dev"], config)
    b = auth_config.resolve(["platform-eng"], config)
    assert a["permissions"] == b["permissions"] == ["dashboard", "dev_workspace"]
    assert a["roleId"] == b["roleId"] == "engineering"


def test_several_org_roles_give_the_union_and_the_top_one_names_the_role():
    config = org_cfg(
        [auth_config.OrgRole(id="engineering", label="Engineering",
                             permissions=["dashboard", "dev_workspace"], priority=50),
         auth_config.OrgRole(id="platform", label="Platform",
                             permissions=["dashboard", "settings"], priority=90)],
        [auth_config.Mapping("AURA-Dev", org_role_id="engineering"),
         auth_config.Mapping("AURA-Plat", org_role_id="platform")])

    r = auth_config.resolve(["AURA-Dev", "AURA-Plat"], config)
    assert set(r["permissions"]) == {"dashboard", "dev_workspace", "settings"}
    assert r["roleId"] == "platform"
    assert r["roleLabel"] == "Platform"


def test_a_mapping_pointing_at_a_deleted_org_role_grants_nothing():
    """Save-time validation rejects this, but a hand-edited DynamoDB row can produce
    it — and the safe reading of a dangling pointer is 'no access', not 'all access'."""
    config = org_cfg([], [auth_config.Mapping("AURA-Dev", org_role_id="gone")])
    assert auth_config.resolve(["AURA-Dev"], config)["permissions"] == []


# ── Upgrading a configuration written before org roles existed ───────────────

def test_a_legacy_role_mapping_resolves_identically_after_the_upgrade():
    """The regression that would lock everyone out of a deployed environment.

    A configuration saved by the previous build must grant exactly what it granted
    before, with no migration step run first.
    """
    from src.services.auth_service import ROLE_PERMISSIONS

    legacy = cfg(Mapping("AURA-Dev", role_id="user_dev", priority=50))
    r = auth_config.resolve(["AURA-Dev"], legacy)

    assert r["permissions"] == ROLE_PERMISSIONS["user_dev"]
    # And the role id is unchanged, so the JWT claim and ROLE_LABELS still resolve.
    assert r["roleId"] == "user_dev"


def test_legacy_groups_naming_the_same_role_collapse_onto_one_org_role():
    legacy = cfg(Mapping("AURA-Dev", role_id="user_dev", priority=50),
                 Mapping("platform-eng", role_id="user_dev", priority=50))
    upgraded = auth_config._upgrade(legacy)

    assert len(upgraded.org_roles) == 1
    assert upgraded.org_roles[0].id == "user_dev"
    assert {m.org_role_id for m in upgraded.mappings} == {"user_dev"}


def test_a_legacy_bare_permission_mapping_keeps_its_extras():
    """A group granted one extra screen on top of a role must not lose it."""
    legacy = cfg(Mapping("AURA-Dev", role_id="user_dev", permissions=["settings"]))
    r = auth_config.resolve(["AURA-Dev"], legacy)
    assert "settings" in r["permissions"]
    assert "dev_workspace" in r["permissions"]


def test_a_legacy_mapping_with_no_role_gets_its_own_org_role():
    legacy = cfg(Mapping("AURA-Contractors", permissions=["dashboard"]))
    upgraded = auth_config._upgrade(legacy)

    assert len(upgraded.org_roles) == 1
    assert upgraded.org_roles[0].permissions == ["dashboard"]
    assert auth_config.resolve(["AURA-Contractors"], legacy)["permissions"] == ["dashboard"]


def test_the_upgrade_leaves_an_already_upgraded_config_alone():
    """It runs on every resolve, so it has to be idempotent."""
    config = org_cfg(
        [auth_config.OrgRole(id="engineering", permissions=["dashboard"])],
        [auth_config.Mapping("AURA-Dev", org_role_id="engineering")])
    once = auth_config._upgrade(config)
    twice = auth_config._upgrade(once)
    assert len(twice.org_roles) == 1


# ── Permissions follow the configuration, not the token ──────────────────────
#
# Permissions used to be a JWT claim, and the token lives 8 hours — so moving
# someone between AD groups left them with their old menus for the working day.

def _token_for(username, groups, monkeypatch):
    from src.services import auth_service
    monkeypatch.setattr(auth_service, "get_settings",
                        lambda: type("S", (), {"jwt_secret": "test-secret",
                                               "jwt_expire_minutes": 480})())
    return auth_service.create_token({
        "username": username, "userId": "u1", "role": "engineering",
        "permissions": ["dashboard"], "groups": groups, "local": False,
    })


def test_the_token_carries_identity_not_permissions(monkeypatch):
    from jose import jwt
    token = _token_for("priya", ["AURA-Dev"], monkeypatch)
    claims = jwt.decode(token, "test-secret", algorithms=["HS256"])

    assert "permissions" not in claims, "permissions in the token is what went stale"
    assert claims["groups"] == ["AURA-Dev"]
    assert claims["sub"] == "priya"


def test_editing_an_org_role_reaches_an_already_signed_in_user(monkeypatch):
    """The point of the change: no re-login, no waiting for expiry."""
    from src.services import auth_config, auth_service

    token = _token_for("priya", ["AURA-Dev"], monkeypatch)
    # Group membership is read from the token here — the directory is not reachable
    # in this test, and _current_groups falls back to the token's groups.
    auth_service.forget_cached_groups()

    narrow = org_cfg([auth_config.OrgRole(id="engineering", permissions=["dashboard"])],
                     [auth_config.Mapping("AURA-Dev", org_role_id="engineering")])
    monkeypatch.setattr(auth_config, "get_config", lambda refresh=False: narrow)
    assert auth_service.verify_token(token)["permissions"] == ["dashboard"]

    wide = org_cfg([auth_config.OrgRole(id="engineering",
                                        permissions=["dashboard", "qa_workspace"])],
                   [auth_config.Mapping("AURA-Dev", org_role_id="engineering")])
    monkeypatch.setattr(auth_config, "get_config", lambda refresh=False: wide)
    auth_service.forget_cached_groups()
    assert auth_service.verify_token(token)["permissions"] == ["dashboard", "qa_workspace"]


def test_losing_every_mapped_group_revokes_access_on_the_next_request(monkeypatch):
    from src.services import auth_config, auth_service

    token = _token_for("priya", ["AURA-Dev"], monkeypatch)
    auth_service.forget_cached_groups()
    monkeypatch.setattr(auth_config, "get_config", lambda refresh=False:
                        org_cfg([auth_config.OrgRole(id="ops", permissions=["logs"])],
                                [auth_config.Mapping("AURA-Ops", org_role_id="ops")]))

    assert auth_service.verify_token(token)["permissions"] == []


def test_a_local_token_resolves_from_roles_not_org_roles(monkeypatch):
    """Break-glass must survive a misconfigured org role — recovering from exactly
    that is what it is for."""
    from src.services import auth_config, auth_service

    monkeypatch.setattr(auth_service, "get_settings",
                        lambda: type("S", (), {"jwt_secret": "test-secret",
                                               "jwt_expire_minutes": 480})())
    monkeypatch.setattr(auth_service, "get_role", lambda rid: None)
    # No org roles at all, and the directory enabled — the worst case.
    monkeypatch.setattr(auth_config, "get_config", lambda refresh=False:
                        org_cfg([], [], enabled=True))

    token = auth_service.create_token({
        "username": "admin", "userId": "u0", "role": "super_admin",
        "permissions": [], "groups": [], "local": True,
    })
    resolved = auth_service.verify_token(token)
    assert "user_management" in resolved["permissions"]


def test_a_token_from_the_previous_build_still_works(monkeypatch):
    """A deploy must not sign everyone out."""
    from datetime import datetime, timedelta, timezone

    from jose import jwt
    from src.services import auth_service

    monkeypatch.setattr(auth_service, "get_settings",
                        lambda: type("S", (), {"jwt_secret": "test-secret",
                                               "jwt_expire_minutes": 480})())
    old_style = jwt.encode(
        {"sub": "priya", "userId": "u1", "role": "user_dev",
         "permissions": ["dashboard", "dev_workspace"],
         "exp": datetime.now(timezone.utc) + timedelta(minutes=60)},
        "test-secret", algorithm="HS256")

    assert auth_service.verify_token(old_style)["permissions"] == [
        "dashboard", "dev_workspace"]


# ── Save-time validation ─────────────────────────────────────────────────────

@pytest.fixture
def admin_api(monkeypatch):
    """A client signed in as someone who may edit the directory configuration."""
    from fastapi.testclient import TestClient
    from src.main import app
    from src.routers.auth import get_current_user

    # Save and restore rather than pop: test_ontology_lens_api installs its own
    # override at module import, and popping ours would take theirs with it.
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: {
        "username": "admin", "userId": "u0", "role": "super_admin",
        "permissions": ["user_management"],
    }
    saved: dict = {}
    monkeypatch.setattr("src.services.auth_config.set_config",
                        lambda enabled, mappings, actor, org_roles=None: saved.update(
                            enabled=enabled, mappings=mappings, orgRoles=org_roles)
                        or __import__("src.services.auth_config", fromlist=["x"]).AuthConfig())
    yield TestClient(app), saved
    if previous is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previous


def _put(client, **body):
    payload = {"enabled": True, "orgRoles": [], "mappings": [], **body}
    return client.put("/auth/ldap/config", json=payload)


ADMIN_ROLE = {"id": "platform", "label": "Platform",
              "permissions": ["dashboard", "user_management"], "priority": 90}


def test_an_org_role_granting_nothing_is_refused(admin_api):
    client, _ = admin_api
    r = _put(client,
             orgRoles=[ADMIN_ROLE, {"id": "empty", "label": "Empty", "permissions": []}],
             mappings=[{"group": "AURA-Admins", "orgRoleId": "platform"}])
    assert r.status_code == 400
    assert "grants no menus" in r.json()["detail"]


def test_a_mapping_pointing_at_an_unknown_org_role_is_refused(admin_api):
    client, _ = admin_api
    r = _put(client, orgRoles=[ADMIN_ROLE],
             mappings=[{"group": "AURA-Admins", "orgRoleId": "platform"},
                       {"group": "AURA-Dev", "orgRoleId": "nope"}])
    assert r.status_code == 400
    assert "unknown organization role" in r.json()["detail"]


def test_duplicate_org_role_ids_are_refused(admin_api):
    client, _ = admin_api
    r = _put(client, orgRoles=[ADMIN_ROLE, dict(ADMIN_ROLE)],
             mappings=[{"group": "AURA-Admins", "orgRoleId": "platform"}])
    assert r.status_code == 400
    assert "share an id" in r.json()["detail"]


def test_a_config_that_locks_every_admin_out_is_refused(admin_api):
    """One save must not be able to remove the only way to undo it."""
    client, _ = admin_api
    r = _put(client,
             orgRoles=[{"id": "engineering", "label": "Engineering",
                        "permissions": ["dashboard"], "priority": 50}],
             mappings=[{"group": "AURA-Dev", "orgRoleId": "engineering"}])
    assert r.status_code == 400
    assert "User Management" in r.json()["detail"]


def test_a_valid_configuration_saves(admin_api):
    client, saved = admin_api
    r = _put(client, orgRoles=[ADMIN_ROLE],
             mappings=[{"group": "AURA-Admins", "orgRoleId": "platform"}])
    assert r.status_code == 200
    assert saved["orgRoles"][0]["id"] == "platform"
    assert saved["mappings"][0]["orgRoleId"] == "platform"
