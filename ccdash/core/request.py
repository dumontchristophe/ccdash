"""What a request is read through: the query parameters, the narrowing they
render to, and the two refusals an unreadable one earns.
"""

import dataclasses
from typing import Any, ClassVar

from . import store

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
    """The three narrowings a request is read through, before rendering.

    Attributes:
        days: Length of the rolling window, in days; 0 is the whole history.
        host: The machine the events were exported from, None for all of them.
        project: The project they belong to, '(undefined)' for the rows carrying
          none, None for all of them.
    """

    days: int
    host: str | None
    project: str | None

    def scope(self, previous: bool = False, window_only: bool = False) -> Scope:
        """The window and scope of a query, as a clause and its values.

        previous=True slides the window back by its own length, so the same
        aggregate run twice compares. Host and project do not move.

        window_only=True keeps just the day window and drops host and project:
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


def one_param(params: dict[str, list[str]], key: str, default: str = "") -> str:
    """First item of a query parameter, since `parse_qs` returns lists."""
    return (params.get(key) or [default])[0]


def int_param(params: dict[str, list[str]], key: str) -> int:
    """An identifier parameter as an int, `0` when it is absent.

    Raises:
        BadRequestError: If the parameter is present but not a number. A
            tolerant parse would answer an empty payload, which reads as "no
            such record".
    """
    raw = one_param(params, key)
    if not raw:
        return 0
    value = store.as_int(raw)
    if value is None:
        raise BadRequestError(key)
    return value
