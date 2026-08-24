"""Merging sign-ups from several platforms into one list of people.

Email is the only key we trust. Two of the five channels won't give us one,
so those sign-ups stay as separate rows and are flagged rather than guessed
at. The failure mode of a fuzzy name match is turning away someone who
really did sign up, at the door, in front of a queue — which is worse than
showing an organiser two rows that might be the same person and letting them
decide.

Where a name match *is* plausible, we surface it as a suggestion the
organiser can confirm. We never apply it ourselves.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


@dataclass
class Source:
    signup_id: str
    channel_id: str
    kind: str
    label: str
    status: str
    created_at: str


@dataclass
class Person:
    key: str
    name: str
    email: str | None
    handle: str | None
    status: str                     # going | waitlist | cancelled
    first_seen: str
    sources: list[Source] = field(default_factory=list)

    @property
    def keyed(self) -> bool:
        """True when we merged on something reliable."""
        return self.email is not None

    @property
    def merged(self) -> bool:
        return len(self.sources) > 1


def normalise_email(email: str | None) -> str | None:
    if not email:
        return None
    e = email.strip().lower()
    return e or None


def _name_key(name: str) -> str:
    """A loose comparison key, used only to *suggest* merges, never to apply."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = re.sub(r"[^a-z ]", " ", n.lower())
    parts = [p for p in n.split() if len(p) > 1]
    return " ".join(sorted(parts))


# Status precedence: if any source says they're coming, they're coming.
_RANK = {"cancelled": 0, "waitlist": 1, "going": 2}


def merge_people(conn, event_id: str) -> list[Person]:
    rows = conn.execute(
        """
        SELECT s.id, s.channel_id, s.external_id, s.name, s.email, s.handle,
               s.status, s.created_at, c.kind, c.label
          FROM signup s JOIN channel c ON c.id = s.channel_id
         WHERE s.event_id = ?
         ORDER BY s.created_at
        """,
        (event_id,),
    ).fetchall()

    people: dict[str, Person] = {}
    for r in rows:
        email = normalise_email(r["email"])
        # Unkeyable sign-ups get a key unique to that row, so they stay distinct.
        key = f"e:{email}" if email else f"x:{r['channel_id']}:{r['external_id']}"

        p = people.get(key)
        if p is None:
            p = Person(
                key=key, name=r["name"] or "(no name)", email=email,
                handle=r["handle"], status=r["status"], first_seen=r["created_at"],
            )
            people[key] = p
        else:
            # Prefer the fuller name; platforms vary in what they collect.
            if len(r["name"] or "") > len(p.name):
                p.name = r["name"]
            if p.handle is None and r["handle"]:
                p.handle = r["handle"]
            if _RANK[r["status"]] > _RANK[p.status]:
                p.status = r["status"]

        p.sources.append(Source(
            signup_id=r["id"], channel_id=r["channel_id"], kind=r["kind"],
            label=r["label"], status=r["status"], created_at=r["created_at"],
        ))

    return sorted(
        people.values(),
        key=lambda p: (_RANK[p.status] * -1, p.first_seen),
    )


def suggest_merges(people: list[Person]) -> list[tuple[Person, Person]]:
    """Pairs that look like the same person but couldn't be merged on email.

    Only ever pairs an unkeyed person with someone else — two rows that both
    have (different) email addresses are two people, however similar the name.
    """
    pairs: list[tuple[Person, Person]] = []
    by_name: dict[str, list[Person]] = {}
    for p in people:
        if p.status == "cancelled":
            continue
        by_name.setdefault(_name_key(p.name), []).append(p)

    for group in by_name.values():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a.keyed and b.keyed:
                    continue          # distinct emails: distinct people
                pairs.append((a, b))
    return pairs


def to_csv_rows(people: list[Person]) -> list[list[str]]:
    out = [["name", "email", "handle", "status", "channels", "first_signed_up",
            "matched_on_email"]]
    for p in people:
        out.append([
            p.name, p.email or "", p.handle or "", p.status,
            " + ".join(sorted({s.label or s.kind for s in p.sources})),
            p.first_seen, "yes" if p.keyed else "no",
        ])
    return out
