from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .models import AUTHORITY_MEMBER_ROLES, AuthorityMemberSpec


class AuthoritySetError(ValueError):
    pass


def member_identity(member: dict[str, Any] | AuthorityMemberSpec) -> dict[str, str]:
    if isinstance(member, AuthorityMemberSpec):
        return {"member_id": member.id, "role": member.role, "path": member.path, "content_sha256": ""}
    return {
        "member_id": str(member.get("member_id", member.get("id", ""))),
        "role": str(member.get("role", "")),
        "path": str(member.get("path", "")),
        "content_sha256": str(member.get("content_sha256", "")),
    }


def validate_member_specs(members: Iterable[AuthorityMemberSpec]) -> None:
    seen: set[str] = set()
    for member in members:
        if not member.id or member.id in seen:
            raise AuthoritySetError(f"duplicate or empty authority member id: {member.id}")
        if member.role not in AUTHORITY_MEMBER_ROLES:
            raise AuthoritySetError(f"unsupported authority member role: {member.role}")
        if not member.path or member.path.startswith("/") or ".." in member.path.split("/"):
            raise AuthoritySetError(f"unsafe authority member path: {member.path}")
        seen.add(member.id)


def aggregate_authority_set_hash(members: Iterable[dict[str, Any]]) -> str:
    normalized = sorted((member_identity(member) for member in members), key=lambda item: (item["member_id"], item["role"], item["path"]))
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def authority_set_revision_id(aggregate_hash: str) -> str:
    return f"AS-REV-{aggregate_hash[:20].upper()}"


def member_change_sets(previous: Iterable[dict[str, Any]], current: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    before = {member_identity(item)["member_id"]: member_identity(item) for item in previous}
    after = {member_identity(item)["member_id"]: member_identity(item) for item in current}
    unchanged = sorted(item for item in before.keys() & after.keys() if before[item] == after[item])
    modified = sorted(item for item in before.keys() & after.keys() if before[item] != after[item])
    return {
        "unchanged": unchanged,
        "modified": modified,
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
    }


def canonical_member_records(members: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(item) for item in members), key=lambda item: (str(item.get("member_id", "")), str(item.get("role", "")), str(item.get("path", ""))))
