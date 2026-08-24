"""Capacity arithmetic.

One room, several booking channels that each think they control it — the
same problem a hotel solves with channel allotments, and it has the same two
answers.

  pool       Every channel is told the full remaining number. Nothing
             strands. Two people signing up in the same second can both get
             in, so the room can go slightly over.

  allocated  Each channel gets a fixed slice it polices alone. Nothing
             oversells. Places strand — one channel sitting on ten unused
             seats while another turns people away.

`oversell_pct` is the deliberate margin for no-shows, which for a free
public meeting is routinely 20-30%. It is applied to the *sellable* number,
never to the number shown as the room's capacity, because those are two
different facts and conflating them is how you end up with people standing
in a corridor.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChannelCount:
    channel_id: str
    kind: str
    label: str
    allocation: int | None
    going: int
    waitlist: int
    enforces_cap: bool          # False for atproto/facebook — count is a signal only


@dataclass
class Capacity:
    capacity: int | None
    oversell_pct: int
    mode: str
    going: int
    waitlist: int
    channels: list[ChannelCount] = field(default_factory=list)

    @property
    def uncapped(self) -> bool:
        return self.capacity is None

    @property
    def sellable(self) -> int | None:
        """Places we will actually hand out, including the no-show margin."""
        if self.capacity is None:
            return None
        return int(self.capacity * (100 + self.oversell_pct) / 100)

    @property
    def remaining(self) -> int | None:
        if self.sellable is None:
            return None
        return max(0, self.sellable - self.going)

    @property
    def full(self) -> bool:
        return self.remaining == 0 if self.sellable is not None else False

    @property
    def pct(self) -> int:
        if not self.sellable:
            return 0
        return min(100, round(self.going / self.sellable * 100))

    @property
    def allocated_total(self) -> int:
        return sum(c.allocation or 0 for c in self.channels if c.enforces_cap)

    @property
    def allocation_drift(self) -> int:
        """Positive = over-committed across channels, negative = under."""
        if self.sellable is None or self.mode != "allocated":
            return 0
        return self.allocated_total - self.sellable

    def remaining_for(self, channel_id: str) -> int | None:
        """What this channel should be told is still available.

        None means "we can't say" — either the event is uncapped, or the
        channel can't enforce anything anyway.
        """
        if self.sellable is None:
            return None
        ch = next((c for c in self.channels if c.channel_id == channel_id), None)
        if ch is None or not ch.enforces_cap:
            return None
        if self.mode == "pool":
            return self.remaining
        alloc = ch.allocation or 0
        return max(0, alloc - ch.going)

    def target_quantity_for(self, channel_id: str) -> int | None:
        """The number to push to the platform as its total capacity.

        Platforms hold a total, not a remainder, so we send
        already-taken + still-available.
        """
        rem = self.remaining_for(channel_id)
        if rem is None:
            return None
        ch = next(c for c in self.channels if c.channel_id == channel_id)
        return ch.going + rem


def read(conn, event_id: str) -> Capacity:
    from .adapters import capability

    ev = conn.execute(
        "SELECT capacity, oversell_pct, capacity_mode FROM event WHERE id = ?",
        (event_id,),
    ).fetchone()
    if ev is None:
        raise KeyError(event_id)

    rows = conn.execute(
        """
        SELECT c.id, c.kind, c.label, c.allocation,
               COALESCE(SUM(s.status = 'going'), 0)    AS going,
               COALESCE(SUM(s.status = 'waitlist'), 0) AS waitlist
          FROM channel c
          LEFT JOIN signup s ON s.channel_id = c.id AND s.status != 'cancelled'
         WHERE c.event_id = ?
         GROUP BY c.id
         ORDER BY c.created_at
        """,
        (event_id,),
    ).fetchall()

    channels = [
        ChannelCount(
            channel_id=r["id"], kind=r["kind"], label=r["label"],
            allocation=r["allocation"], going=r["going"], waitlist=r["waitlist"],
            enforces_cap=capability(r["kind"], "capacity") == "yes",
        )
        for r in rows
    ]

    # The headline count is deduplicated people, not rows: somebody who signed
    # up on two platforms occupies one chair.
    from .merge import merge_people

    people = merge_people(conn, event_id)
    going = sum(1 for p in people if p.status == "going")
    waiting = sum(1 for p in people if p.status == "waitlist")

    return Capacity(
        capacity=ev["capacity"], oversell_pct=ev["oversell_pct"],
        mode=ev["capacity_mode"], going=going, waitlist=waiting, channels=channels,
    )
