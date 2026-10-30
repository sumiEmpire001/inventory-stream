import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from inventory_stream import Inventory, StockError


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "stock.db"
        self.stock = Inventory(self.path)
        self.stock.create_item("part")
        self.stock.adjust("part", 10, actor="admin", reason="opening", command_key="open")

    def reserve(self, key="hold", amount=3):
        return self.stock.reserve("part", amount, owner="owner", command_key=key)

    def test_consume_updates_all_models_and_reconciles(self):
        reservation = self.reserve()
        self.stock.finish(reservation["id"], actor="owner", action="consume", command_key="consume")
        self.assertEqual(self.stock.snapshot("part"), {"sku": "part", "on_hand": 7, "reserved": 0, "available": 7})
        self.assertTrue(self.stock.reconcile()["valid"])

    def test_release_restores_availability_without_changing_stock(self):
        reservation = self.reserve()
        self.stock.finish(reservation["id"], actor="owner", action="release", command_key="release")
        self.assertEqual(self.stock.snapshot("part")["available"], 10)
        self.assertTrue(self.stock.reconcile()["valid"])

    def test_concurrent_reservations_cannot_oversell(self):
        def attempt(n):
            try:
                return self.reserve(str(n), 1)
            except StockError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(attempt, range(30)))
        self.assertEqual(sum(row is not None for row in results), 10)
        self.assertEqual(self.stock.snapshot("part")["available"], 0)

    def test_duplicate_commands_do_not_duplicate_stock_or_ledger(self):
        first = self.reserve()
        second = self.reserve()
        self.assertEqual(first, second)
        self.assertEqual(len(self.stock.entries()), 2)
        self.stock.finish(first["id"], actor="owner", action="consume", command_key="consume")
        self.stock.finish(first["id"], actor="owner", action="consume", command_key="consume")
        self.assertEqual(len(self.stock.entries()), 3)

    def test_conflicting_command_reuse_fails(self):
        self.reserve()
        with self.assertRaises(ValueError):
            self.reserve(amount=4)

    def test_adjustment_cannot_remove_reserved_stock(self):
        self.reserve(amount=8)
        with self.assertRaises(StockError):
            self.stock.adjust("part", -3, actor="admin", reason="loss", command_key="loss")
        self.assertEqual(self.stock.snapshot("part")["on_hand"], 10)

    def test_owner_and_terminal_state_checks(self):
        reservation = self.reserve()
        with self.assertRaises(PermissionError):
            self.stock.finish(reservation["id"], actor="outsider", action="consume", command_key="attack")
        self.stock.finish(reservation["id"], actor="owner", action="release", command_key="release")
        with self.assertRaises(StockError):
            self.stock.finish(reservation["id"], actor="owner", action="consume", command_key="late")

    def test_racing_release_and_consume_have_one_winner(self):
        reservation = self.reserve()
        def finish(action):
            try:
                return self.stock.finish(reservation["id"], actor="owner", action=action, command_key=action)
            except StockError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(finish, ("release", "consume")))
        self.assertEqual(sum(row is not None for row in results), 1)
        self.assertTrue(self.stock.reconcile()["valid"])

    def test_restart_and_pagination(self):
        self.reserve()
        again = Inventory(self.path)
        self.assertEqual(again.snapshot("part")["reserved"], 3)
        self.assertEqual(again.entries(limit=1, offset=1)[0]["kind"], "reserve")

    def test_reconciliation_detects_read_model_damage(self):
        with self.stock.db(True) as db:
            db.execute("UPDATE items SET on_hand=11 WHERE sku='part'")
        self.assertFalse(self.stock.reconcile()["valid"])

    def test_invalid_values_leave_ledger_unchanged(self):
        for amount in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                self.reserve(amount=amount)
        with self.assertRaises(ValueError):
            self.stock.adjust("part", 1, actor="admin", reason="", command_key="bad")
        self.assertEqual(len(self.stock.entries()), 1)

    def test_concurrent_identical_commands_have_one_effect(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            reservations = list(pool.map(lambda _: self.reserve(), range(10)))
        self.assertEqual(len({row["id"] for row in reservations}), 1)
        self.assertTrue(self.stock.reconcile()["valid"])
