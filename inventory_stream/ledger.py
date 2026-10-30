import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


class StockError(ValueError):
    pass


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("Identifiers and reasons must be nonempty strings of at most 256 characters")


def quantity(value, positive=False):
    if type(value) is not int or value == 0 or abs(value) > 1_000_000_000 or (positive and value < 0):
        raise ValueError("Quantity must be a nonzero bounded integer, positive for reservations")


class Inventory:
    def __init__(self, path="stock.db", clock=time.time):
        if str(path) == ":memory:":
            raise ValueError("Use a filesystem database")
        self.path, self.clock = str(path), clock
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS items(sku TEXT PRIMARY KEY,on_hand INTEGER NOT NULL DEFAULT 0,
                    reserved INTEGER NOT NULL DEFAULT 0,CHECK(on_hand>=0 AND reserved>=0 AND reserved<=on_hand));
                CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,sku TEXT NOT NULL REFERENCES items(sku),
                    owner TEXT NOT NULL,quantity INTEGER NOT NULL,state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entries(seq INTEGER PRIMARY KEY,sku TEXT NOT NULL REFERENCES items(sku),
                    kind TEXT NOT NULL,on_hand_delta INTEGER NOT NULL,reserved_delta INTEGER NOT NULL,
                    reference TEXT,actor TEXT NOT NULL,reason TEXT NOT NULL,at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL);
            """)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def create_item(self, sku):
        text(sku)
        with self.db(True) as db:
            db.execute("INSERT INTO items(sku) VALUES(?)", (sku,))

    def snapshot(self, sku):
        with self.db() as db:
            row = db.execute("SELECT * FROM items WHERE sku=?", (sku,)).fetchone()
            if not row:
                raise ValueError("Item not found")
            return dict(row) | {"available": row["on_hand"] - row["reserved"]}

    def _command(self, db, command_key, fingerprint):
        text(command_key)
        row = db.execute("SELECT * FROM commands WHERE id=?", (command_key,)).fetchone()
        packed = json.dumps(fingerprint, sort_keys=True)
        if row and row["fingerprint"] != packed:
            raise ValueError("Command key already belongs to a different operation")
        return packed, json.loads(row["result"]) if row else None

    def _commit(self, db, command_key, fingerprint, result, sku, kind, on_hand, reserved, reference, actor, reason):
        db.execute("INSERT INTO entries(sku,kind,on_hand_delta,reserved_delta,reference,actor,reason,at) VALUES(?,?,?,?,?,?,?,?)", (sku, kind, on_hand, reserved, reference, actor, reason, self.clock()))
        db.execute("INSERT INTO commands VALUES(?,?,?)", (command_key, fingerprint, json.dumps(result)))
        return result

    def adjust(self, sku, delta, *, actor, reason, command_key):
        quantity(delta)
        text(actor)
        text(reason)
        with self.db(True) as db:
            fingerprint, old = self._command(db, command_key, ["adjust", sku, delta, actor, reason])
            if old is not None:
                return old
            row = db.execute("SELECT * FROM items WHERE sku=?", (sku,)).fetchone()
            if not row:
                raise ValueError("Item not found")
            if row["on_hand"] + delta < row["reserved"]:
                raise StockError("Adjustment would consume reserved or unavailable stock")
            db.execute("UPDATE items SET on_hand=on_hand+? WHERE sku=?", (delta, sku))
            result = {"sku": sku, "on_hand": row["on_hand"] + delta, "reserved": row["reserved"]}
            return self._commit(db, command_key, fingerprint, result, sku, "adjust", delta, 0, None, actor, reason)

    def reserve(self, sku, amount, *, owner, command_key):
        quantity(amount, positive=True)
        text(owner)
        with self.db(True) as db:
            fingerprint, old = self._command(db, command_key, ["reserve", sku, amount, owner])
            if old is not None:
                return old
            row = db.execute("SELECT * FROM items WHERE sku=?", (sku,)).fetchone()
            if not row or row["on_hand"] - row["reserved"] < amount:
                raise StockError("Insufficient available stock")
            reference = uuid.uuid4().hex
            db.execute("INSERT INTO reservations VALUES(?,?,?,?,'held')", (reference, sku, owner, amount))
            db.execute("UPDATE items SET reserved=reserved+? WHERE sku=?", (amount, sku))
            result = {"id": reference, "sku": sku, "owner": owner, "quantity": amount, "state": "held"}
            return self._commit(db, command_key, fingerprint, result, sku, "reserve", 0, amount, reference, owner, "reservation")

    def finish(self, reservation_id, *, actor, action, command_key):
        text(actor)
        if action not in ("release", "consume"):
            raise ValueError("Action must be release or consume")
        with self.db(True) as db:
            fingerprint, old = self._command(db, command_key, [action, reservation_id, actor])
            if old is not None:
                return old
            row = db.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            if not row or row["owner"] != actor:
                raise PermissionError("Reservation unavailable to actor")
            if row["state"] != "held":
                raise StockError("Reservation is already terminal")
            delta = -row["quantity"] if action == "consume" else 0
            state = "consumed" if action == "consume" else "released"
            db.execute("UPDATE items SET on_hand=on_hand+?,reserved=reserved-? WHERE sku=?", (delta, row["quantity"], row["sku"]))
            db.execute("UPDATE reservations SET state=? WHERE id=?", (state, reservation_id))
            result = dict(row) | {"state": state}
            return self._commit(db, command_key, fingerprint, result, row["sku"], action, delta, -row["quantity"], reservation_id, actor, action)

    def entries(self, *, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..100 and offset nonnegative")
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM entries ORDER BY seq LIMIT ? OFFSET ?", (limit, offset))]

    def reconcile(self):
        with self.db() as db:
            db.execute("BEGIN")
            findings = []
            for item in db.execute("SELECT * FROM items ORDER BY sku").fetchall():
                totals = db.execute("SELECT COALESCE(SUM(on_hand_delta),0),COALESCE(SUM(reserved_delta),0) FROM entries WHERE sku=?", (item["sku"],)).fetchone()
                held = db.execute("SELECT COALESCE(SUM(quantity),0) FROM reservations WHERE sku=? AND state='held'", (item["sku"],)).fetchone()[0]
                valid = totals[0] == item["on_hand"] and totals[1] == item["reserved"] == held
                findings.append({"sku": item["sku"], "valid": valid, "on_hand": item["on_hand"], "reserved": item["reserved"], "ledger_on_hand": totals[0], "ledger_reserved": totals[1], "held_units": held})
            return {"valid": all(row["valid"] for row in findings), "items": findings}
