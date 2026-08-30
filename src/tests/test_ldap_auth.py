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
