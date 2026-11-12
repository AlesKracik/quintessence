"""Seat reservations for one event. In-memory; one service per event."""

HOLD_TTL = 2    # time units a hold stays valid
MAX_HOLDS = 2   # seats one customer may hold at the same time


class ReservationError(Exception):
    pass


class ReservationService:
    def __init__(self, seats, now):
        self._now = now
        # seat -> None | ("held", customer, valid_until) | ("sold", customer)
        self._seats = {seat: None for seat in seats}

    def _entry(self, seat):
        entry = self._seats[seat]
        if entry and entry[0] == "held" and entry[2] <= self._now():
            self._seats[seat] = entry = None   # the hold ran out
        return entry

    def hold(self, seat, customer):
        if self._entry(seat) is not None:
            raise ReservationError("seat is not available")
        held = [s for s in self._seats if (e := self._entry(s)) and e[0] == "held" and e[1] == customer]
        if len(held) >= MAX_HOLDS:
            raise ReservationError("too many seats on hold")
        self._seats[seat] = ("held", customer, self._now() + HOLD_TTL)

    def confirm(self, seat, customer):
        entry = self._entry(seat)
        if not entry or entry[0] != "held" or entry[1] != customer:
            raise ReservationError("no hold to confirm")
        self._seats[seat] = ("sold", customer)

    def cancel(self, seat, customer):
        entry = self._entry(seat)
        if not entry or entry[1] != customer:
            raise ReservationError("nothing to cancel")
        self._seats[seat] = None

    def status(self, seat):
        entry = self._entry(seat)
        return entry[0] if entry else "free"

    def owner(self, seat):
        entry = self._entry(seat)
        return entry[1] if entry else None
