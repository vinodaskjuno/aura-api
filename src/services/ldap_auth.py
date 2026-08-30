"""Authenticate against LDAP / Active Directory and resolve group membership.

Returns groups; it does not decide permissions. Mapping groups to permissions is
`auth_config`'s job, kept separate so a second identity source (OIDC, say) could be
added later without touching either the mapping or anything downstream of it.

Three details decide whether this works against a real directory rather than only
against a test fixture:

  Injection    The username goes into a search filter. Unescaped, `*)(objectClass=*`
               is an authentication bypass, so every value is escaped.

  Group        Directories disagree on where membership lives, so three strategies
  discovery    are tried in turn (see resolve_groups):
                 1. AD's matching rule 1.2.840.113556.1.4.1941 — transitive, so a
                    user in AURA-Dev via a parent group is found.
                 2. `memberOf` on the user entry — AD, or OpenLDAP with the overlay.
                 3. Reverse search for groups whose member/uniqueMember/memberUid
                    names the user.
               Step 3 is not optional: stock OpenLDAP records membership on the GROUP
               and has no memberOf overlay, so without it every user on a non-AD
               directory authenticates and is then refused for having no groups.
               Verified against OpenLDAP 1.5.0, which needs exactly that path.

  Transport    A simple bind sends the password in clear text. TLS is required unless
               explicitly waived for a lab, and the waiver warns every time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# AD's LDAP_MATCHING_RULE_IN_CHAIN: walks nested group membership server-side.
AD_IN_CHAIN = "1.2.840.113556.1.4.1941"

USER_ATTRS = ["cn", "displayName", "mail", "memberOf", "objectGUID",
              "sAMAccountName", "uid", "userPrincipalName"]

CONNECT_TIMEOUT_S = 5


class LdapError(Exception):
    """Directory could not be reached or refused the service account.

    Distinct from bad user credentials on purpose: this one means fall back to
    break-glass, whereas a wrong password must never fall back to anything.
    """


@dataclass
class LdapUser:
    username: str
    dn: str
    display_name: str = ""
    email: str = ""
    object_guid: str = ""
    groups: list[str] = field(default_factory=list)   # CNs, not DNs

    @property
    def user_id(self) -> str:
        """Stable across renames, and visibly a directory identity in audit trails."""
        return f"ldap:{self.object_guid or self.username}"


def _escape(value: str) -> str:
    from ldap3.utils.conv import escape_filter_chars
    return escape_filter_chars(value or "")


def cn_of(dn: str) -> str:
    """'CN=AURA-Dev,OU=Groups,DC=x' -> 'AURA-Dev'.

    Mappings are written as group names because an administrator should not have to
    paste a full DN to grant access.
    """
    head = (dn or "").split(",")[0].strip()
    return head[3:] if head[:3].upper() == "CN=" else head


def _server(settings):
    from ldap3 import Server, Tls
    import ssl

    uri = (settings.ldap_uri or "").strip()
    use_ssl = uri.lower().startswith("ldaps://")

    if not use_ssl and not settings.ldap_allow_insecure:
        raise LdapError(
            "Refusing a plaintext LDAP bind: the password would cross the network in "
            "clear text. Use ldaps:// or set LDAP_ALLOW_INSECURE=true for a lab server.")
    if not use_ssl:
        log.warning("LDAP is configured WITHOUT TLS (%s). Credentials cross the "
                    "network in clear text. Do not use this outside a lab.", uri)

    tls = Tls(validate=ssl.CERT_REQUIRED) if use_ssl else None
    return Server(uri, use_ssl=use_ssl, tls=tls, get_info=None,
                  connect_timeout=CONNECT_TIMEOUT_S)


def _service_connection(settings, server=None):
    """Bind as the read-only service account used to look users up."""
    from ldap3 import Connection

    try:
        conn = Connection(
            server or _server(settings),
            user=settings.ldap_bind_dn or None,
            password=settings.ldap_bind_password or None,
            auto_bind=True, receive_timeout=CONNECT_TIMEOUT_S,
        )
    except LdapError:
        raise
    except Exception as exc:  # noqa: BLE001 — ldap3 raises a wide range here
        raise LdapError(f"Could not bind as the service account: {exc}") from exc
    return conn


def _search_user(conn, settings, username: str) -> dict | None:
    """Find the user entry. `username` is escaped before it reaches the filter."""
    safe = _escape(username)
    template = settings.ldap_user_filter or "(sAMAccountName={username})"
    user_filter = template.replace("{username}", safe)

    conn.search(search_base=settings.ldap_base_dn, search_filter=user_filter,
                attributes=USER_ATTRS)
    if not conn.entries:
        return None
    entry = conn.entries[0]
    return {
        "dn": entry.entry_dn,
        "attrs": {a: entry[a].value for a in USER_ATTRS if a in entry},
    }


def _nested_groups(conn, settings, user_dn: str) -> list[str] | None:
    """Groups held transitively, via AD's matching rule.

    Returns None when the directory does not support the rule (OpenLDAP), so the
    caller falls back rather than treating it as "no groups".
    """
    try:
        ok = conn.search(
            search_base=settings.ldap_base_dn,
            search_filter=f"(member:{AD_IN_CHAIN}:={_escape(user_dn)})",
            attributes=["cn"])
        if not ok or not conn.entries:
            return None
        return [cn_of(e.entry_dn) for e in conn.entries]
    except Exception as exc:  # noqa: BLE001 — unsupported rule is not a failure
        log.debug("nested-group search unsupported or failed (%s); falling back", exc)
        return None


def _reverse_group_search(conn, settings, user_dn: str, uid: str = "") -> list[str]:
    """Find groups that list this user as a member.

    Required for OpenLDAP, FreeIPA and Samba: `groupOfNames` records membership on
    the GROUP, and `memberOf` only exists if the memberof overlay is enabled — which
    it is not by default. Without this, every user on a non-AD directory
    authenticates successfully and is then refused for having no groups.

    Three membership attributes are covered because the schema varies:
    groupOfNames uses `member`, groupOfUniqueNames uses `uniqueMember`, and posixGroup
    uses `memberUid` (a bare username, not a DN).
    """
    dn = _escape(user_dn)
    clauses = [f"(member={dn})", f"(uniqueMember={dn})"]
    if uid:
        clauses.append(f"(memberUid={_escape(uid)})")
    try:
        conn.search(search_base=settings.ldap_base_dn,
                    search_filter=f"(|{''.join(clauses)})",
                    attributes=["cn"])
        return [cn_of(e.entry_dn) for e in conn.entries]
    except Exception as exc:  # noqa: BLE001
        log.debug("reverse group search failed: %s", exc)
        return []


def resolve_groups(conn, settings, user_dn: str, attrs: dict) -> tuple[list[str], str]:
    """A user's groups, trying each strategy until one yields something.

    Ordered by directory capability, not preference: AD answers transitively, an
    overlay-enabled directory answers via memberOf, and everything else needs the
    reverse search.
    """
    nested = _nested_groups(conn, settings, user_dn)
    if nested:
        return sorted({g for g in nested if g}), "nested (AD matching rule)"

    member_of = attrs.get("memberOf") or []
    if isinstance(member_of, str):
        member_of = [member_of]
    if member_of:
        return sorted({cn_of(dn) for dn in member_of if dn}), "memberOf"

    uid = str(attrs.get("sAMAccountName") or attrs.get("uid") or "")
    reverse = _reverse_group_search(conn, settings, user_dn, uid)
    return sorted({g for g in reverse if g}), "reverse member search"


def _guid(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        import uuid
        try:
            return str(uuid.UUID(bytes_le=raw))
        except (ValueError, TypeError):
            return raw.hex()
    return str(raw)


def authenticate(username: str, password: str, settings=None) -> LdapUser | None:
    """Verify a password against the directory and return the user with their groups.

    Returns None for bad credentials. Raises LdapError when the *directory* is the
    problem — the caller must be able to tell "wrong password" (never fall back)
    from "AD is down" (fall back to break-glass).
    """
    from ldap3 import Connection

    if settings is None:
        from src.config_settings import get_settings
        settings = get_settings()

    if not username or not password:
        return None            # an empty password is an anonymous bind, which succeeds

    server = _server(settings)
    conn = _service_connection(settings, server)
    try:
        found = _search_user(conn, settings, username)
        if not found:
            log.info("LDAP: no entry for %r", username)
            return None

        # The real credential check: rebind as the user themselves.
        try:
            user_conn = Connection(server, user=found["dn"], password=password,
                                   auto_bind=True, receive_timeout=CONNECT_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — a failed bind is a wrong password
            log.info("LDAP: bind failed for %r", username)
            return None
        user_conn.unbind()

        attrs = found["attrs"]
        groups, _source = resolve_groups(conn, settings, found["dn"], attrs)

        return LdapUser(
            username=str(attrs.get("sAMAccountName") or username),
            dn=found["dn"],
            display_name=str(attrs.get("displayName") or attrs.get("cn") or username),
            email=str(attrs.get("mail") or ""),
            object_guid=_guid(attrs.get("objectGUID")),
            groups=groups,
        )
    finally:
        try:
            conn.unbind()
        except Exception:  # noqa: BLE001
            pass


def test_connection(settings=None) -> dict:
    """Bind with the service account and report what happened, for the UI's
    Test Connection button. Returns the real LDAP error rather than a generic
    failure, because that message is what an administrator has to act on."""
    if settings is None:
        from src.config_settings import get_settings
        settings = get_settings()
    try:
        conn = _service_connection(settings)
        conn.unbind()
        return {"ok": True, "message": f"Bound successfully to {settings.ldap_uri}"}
    except LdapError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


def lookup_groups(username: str, settings=None) -> dict:
    """Groups for a user WITHOUT their password — powers the admin preview.

    Deliberately separate from authenticate(): previewing someone's access must not
    require knowing their password.
    """
    if settings is None:
        from src.config_settings import get_settings
        settings = get_settings()
    try:
        conn = _service_connection(settings)
    except LdapError as exc:
        return {"ok": False, "message": str(exc), "groups": []}
    try:
        found = _search_user(conn, settings, username)
        if not found:
            return {"ok": False, "message": f"No directory entry for {username!r}",
                    "groups": []}
        groups, source = resolve_groups(conn, settings, found["dn"], found["attrs"])
        return {"ok": True, "dn": found["dn"], "groupSource": source, "groups": groups}
    finally:
        try:
            conn.unbind()
        except Exception:  # noqa: BLE001
            pass
