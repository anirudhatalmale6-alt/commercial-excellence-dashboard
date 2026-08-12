"""
Role-based scoping.

One rule, applied once, in one place: a scope is a set of user ids plus a label.

    rep      -> just themselves
    manager  -> themselves + every user reporting to them (recursively)
    admin    -> everyone

A manager may additionally drill into one of their reports; that is validated
here so no route can be tricked into returning another team's numbers by
passing a user_id in a query string.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Dataset, Role, User


@dataclass
class Scope:
    label: str
    user_ids: set[str]
    viewer: User
    is_team_view: bool
    focus_user: User | None = None

    def owns(self, owner_id: str) -> bool:
        return owner_id in self.user_ids


def _descendants(ds: Dataset, root_id: str) -> set[str]:
    children: dict[str, list[str]] = {}
    for u in ds.users:
        if u.manager_id:
            children.setdefault(u.manager_id, []).append(u.id)
    out: set[str] = set()
    stack = [root_id]
    while stack:
        cur = stack.pop()
        for c in children.get(cur, []):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def direct_reports(ds: Dataset, manager_id: str) -> list[User]:
    return [u for u in ds.users if u.manager_id == manager_id]


def build_scope(ds: Dataset, viewer: User, focus_user_id: str | None = None) -> Scope:
    if viewer.role == Role.ADMIN:
        allowed = {u.id for u in ds.users}
        label = "All teams"
        team_view = True
    elif viewer.role == Role.MANAGER:
        allowed = _descendants(ds, viewer.id) | {viewer.id}
        label = viewer.team
        team_view = True
    else:
        allowed = {viewer.id}
        label = viewer.name
        team_view = False

    focus: User | None = None
    if focus_user_id:
        if focus_user_id not in allowed:
            raise PermissionError("user is outside your scope")
        focus = next((u for u in ds.users if u.id == focus_user_id), None)
        if focus is not None:
            allowed = {focus.id}
            label = focus.name
            team_view = False

    return Scope(label=label, user_ids=allowed, viewer=viewer, is_team_view=team_view, focus_user=focus)
