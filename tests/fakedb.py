"""An in-memory stand-in for Supabase that can be told to lag behind.

The point is the lagging. Fynn's schema grows faster than the database it is
pointed at, and every bug of this kind has been the same shape: a write carries
a column the database has not got, the row is lost whole, and the failure
surfaces somewhere unrelated — a foreign key, an empty screen, a ledger that
will not connect. Testing only against an up-to-date schema cannot see any of
it, which is exactly why it kept reaching production.

`missing_columns` and `missing_tables` reproduce that state deliberately.
"""
from __future__ import annotations


class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count if count is not None else len(data)


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self._filters: list[tuple[str, object]] = []
        self._op = None
        self._payload = None
        self._limit = None

    # -- builders ------------------------------------------------------------
    def select(self, *_a, **kw):
        self._op = "select"
        self._count = kw.get("count")
        return self

    def upsert(self, payload, **_kw):
        self._op, self._payload = "upsert", payload
        return self

    def insert(self, payload, **_kw):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload, **_kw):
        self._op, self._payload = "update", payload
        return self

    def delete(self, **_kw):
        self._op = "delete"
        return self

    def eq(self, column, value):
        self._filters.append((column, value))
        return self

    def limit(self, n):
        self._limit = n
        return self

    # -- execution -----------------------------------------------------------
    def _matches(self, row) -> bool:
        return all(row.get(c) == v for c, v in self._filters)

    def execute(self):
        self.db.guard_table(self.table)
        rows = self.db.rows.setdefault(self.table, [])

        if self._op == "select":
            hit = [r for r in rows if self._matches(r)]
            return _Result(hit[: self._limit] if self._limit else hit, count=len(hit))

        if self._op == "delete":
            keep = [r for r in rows if not self._matches(r)]
            self.db.rows[self.table] = keep
            return _Result([])

        if self._op == "update":
            self.db.guard_columns(self.table, self._payload)
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
            return _Result([])

        payloads = self._payload if isinstance(self._payload, list) else [self._payload]
        for payload in payloads:
            self.db.guard_columns(self.table, payload)
            self.db.check_foreign_keys(self.table, payload)
            key = self.db.key_for(self.table, payload)
            if self._op == "upsert" and key is not None:
                for r in rows:
                    if self.db.key_for(self.table, r) == key:
                        r.update(payload)
                        break
                else:
                    rows.append(dict(payload))
            else:
                rows.append(dict(payload))
        return _Result([dict(p) for p in payloads])


class FakeDB:
    """Rows, primary keys and the foreign keys that actually bite."""

    # Only the keys that matter to the behaviour under test.
    KEYS = {
        "users": ("id",),
        "firm_profiles": ("workspace_id",),
        "firm_rules": ("firm_id", "platform", "label"),
        "settlement_files": ("id",),
        "account_mappings": ("firm_id", "ledger", "fynn_account"),
        "ledger_connections": ("firm_id", "ledger"),
        "cycle_resolutions": ("firm_id", "cycle", "key"),
    }
    # Every scoped table hangs off the workspace row. This is the constraint
    # that turned a dropped firm_profiles write into "Xero will not connect".
    CHILDREN = ("firm_rules", "settlement_files", "account_mappings",
                "ledger_connections", "cycle_resolutions", "posted_entries")

    def __init__(self, missing_columns=None, missing_tables=()):
        self.rows: dict[str, list[dict]] = {}
        self.missing_columns = missing_columns or {}
        self.missing_tables = set(missing_tables)

    def table(self, name):
        return _Query(self, name)

    def guard_table(self, table):
        if table in self.missing_tables:
            raise Exception(
                f"Could not find the table 'public.{table}' in the schema cache")

    def guard_columns(self, table, payload):
        for column in self.missing_columns.get(table, ()):
            if column in payload:
                raise Exception(
                    f"Could not find the '{column}' column of '{table}' "
                    f"in the schema cache")

    def check_foreign_keys(self, table, payload):
        if table not in self.CHILDREN:
            return
        parent = payload.get("firm_id")
        known = {r.get("workspace_id") for r in self.rows.get("firm_profiles", [])}
        if parent is not None and parent not in known:
            raise Exception(
                f'insert or update on table "{table}" violates foreign key '
                f'constraint "{table}_firm_id_fkey"')

    def key_for(self, table, row):
        cols = self.KEYS.get(table)
        if not cols:
            return None
        return tuple(row.get(c) for c in cols)

    def count(self, table):
        return len(self.rows.get(table, []))


def install(db):
    """Point services.database at this fake, and reset Fynn's caches."""
    import services.database as database
    import main

    database._get_client = lambda: db
    main._profiles._profiles.clear()
    main._workspaces.clear()
    main._users._by_id.clear()
    main._users._unsaved.clear()
    main._users._loaded = False
    return db
