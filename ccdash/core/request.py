"""What a request is read through: the query parameters, the narrowing they
render to, and the two refusals an unreadable one earns.
"""

import dataclasses
import datetime
import re
from typing import Any, ClassVar

from . import store, tz

NOT_FOUND = "not found"

# The answer to a request the server could not read. Deliberately says nothing
# of which parameter or why: the caller sent the request and holds that half.
BAD_REQUEST = "bad request"


class NotFoundError(Exception):
    """An endpoint asked for a record the database does not hold.

    A 404 carrying NOT_FOUND rather than a payload: a detail's aggregates answer
    a row of NULLs for an unknown id, which no view can tell from an empty one.
    """


class BadRequestError(Exception):
    """A request parameter the server could not read.

    A 400 carrying BAD_REQUEST, which keeps a malformed query out of the 500s.
    """


@dataclasses.dataclass(frozen=True)
class Scope:
    """What a query is narrowed to, rendered: a SQL clause and its values.

    The clause starts with " AND " and is empty when nothing narrows, so it
    appends to a query whose WHERE is already open. `aggregates.scoped` renders
    the two into a query and repeats `args` once per `{scope}` marker, so no
    call site guesses `args * N`.

    There is no empty default: an unnarrowed query is named `Scope.UNBOUNDED`,
    never an omitted argument, so a full-table scan is a choice and not a slip.

    Attributes:
        clause: The SQL fragment.
        args: The values its placeholders consume, in order.
    """

    UNBOUNDED: ClassVar["Scope"]

    clause: str
    args: tuple[Any, ...]

    def narrow(self, clause: str, *args: Any) -> "Scope":
        """This scope with one more condition on its end."""
        return Scope(self.clause + clause, self.args + args)


# The whole store, named: the only way to a query that no window bounds.
Scope.UNBOUNDED = Scope("", ())


@dataclasses.dataclass(frozen=True)
class Filters:
    """The narrowings a request is read through, before rendering.

    The time window is either the rolling `days` or the explicit date range;
    `from_params` zeroes `days` when a range is present, so the two never
    render together.

    Attributes:
        days: Length of the rolling window, in days; 0 is the whole history.
        host: The machine the events were exported from, None for all of them.
        project: The project they belong to, '(undefined)' for the rows carrying
          none, None for all of them.
        start_date: First day of the range, `YYYY-MM-DD` in the configured
          zone, None for no lower bound.
        end_date: Last day of the range, included whole, None for no upper
          bound.
    """

    days: int
    host: str | None
    project: str | None
    start_date: str | None = None
    end_date: str | None = None

    @classmethod
    def from_params(cls, params: dict[str, list[str]]) -> "Filters":
        """The filters a query string carries.

        An absent `days` is 0, the whole history: there is no server-side
        default. A range wins over `days`, which is zeroed rather than refused.

        Raises:
            BadRequestError: If `days` is not a number, a date is not
                `YYYY-MM-DD`, or `start_date` is after `end_date`.
        """
        start = _date_param(params, "start_date")
        end = _date_param(params, "end_date")
        if start and end and start > end:
            raise BadRequestError("start_date")
        days = 0 if start or end else int_param(params, "days")
        return cls(
            days=days,
            host=one_param(params, "host") or None,
            project=one_param(params, "project") or None,
            start_date=start or None,
            end_date=end or None,
        )

    def scope(self, previous: bool = False, window_only: bool = False) -> Scope:
        """The window and scope of a query, as a clause and its values.

        previous=True slides the rolling window back by its own length, so the
        same aggregate run twice compares. Host and project do not move, and
        a date range does not slide: it has no earlier window to compare with.

        window_only=True keeps just the time window and drops host and project:
        the Diagnostics page bounds its scans by time but stays global across
        machines and projects, so it still surfaces the misconfigured one."""
        # `%%s` is SQLite's own `%s`, doubled to survive `%` formatting.
        conditions: list[str] = []
        args: list[Any] = []
        if self.days:
            if previous:
                conditions.append(
                    "ts >= strftime('%%s','now','-%d days') AND "
                    "ts < strftime('%%s','now','-%d days')" % (2 * self.days, self.days)
                )
            else:
                conditions.append("ts >= strftime('%%s','now','-%d days')" % self.days)
        if self.start_date:
            conditions.append("ts >= ?")
            args.append(tz.date_to_epoch(self.start_date))
        if self.end_date:
            conditions.append("ts < ?")
            args.append(tz.date_to_epoch(_next_day(self.end_date)))
        for column, value in (("host", self.host), ("project", self.project)):
            if window_only or not value:
                continue
            # api_projects groups the projectless rows under this label, so
            # the filter has to target IS NULL and not the literal.
            if value == "(undefined)":
                conditions.append("%s IS NULL" % column)
                continue
            conditions.append("%s = ?" % column)
            args.append(value)
        clause = (" AND " + " AND ".join(conditions)) if conditions else ""
        return Scope(clause, tuple(args))


def _date_param(params: dict[str, list[str]], key: str) -> str:
    """A `YYYY-MM-DD` parameter as sent, `""` when it is absent.

    Raises:
        BadRequestError: If the parameter is present in any other shape. The
            match is strict on the zero-padded form, so the validated strings
            compare as dates.
    """
    raw = one_param(params, key)
    if not raw:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise BadRequestError(key)
    try:
        datetime.date.fromisoformat(raw)
    except ValueError as err:
        raise BadRequestError(key) from err
    return raw


def _next_day(date: str) -> str:
    """The calendar day after a `YYYY-MM-DD` date, as the same string.

    Stepping the date rather than adding 86400 to its midnight keeps the bound
    on a DST switch, where the day is 23 or 25 hours long.
    """
    return (datetime.date.fromisoformat(date) + datetime.timedelta(days=1)).isoformat()


def one_param(params: dict[str, list[str]], key: str, default: str = "") -> str:
    """First item of a query parameter, since `parse_qs` returns lists."""
    return (params.get(key) or [default])[0]


def int_param(params: dict[str, list[str]], key: str) -> int:
    """An integer parameter, `0` when it is absent.

    `0` is whatever the caller makes of it: no such record for an id, the
    whole history for `days`.

    Raises:
        BadRequestError: If the parameter is present but not a number. A
            tolerant parse would answer an empty payload or a full scan, and
            neither reads as "you sent a typo".
    """
    raw = one_param(params, key)
    if not raw:
        return 0
    value = store.as_int(raw)
    if value is None:
        raise BadRequestError(key)
    return value
