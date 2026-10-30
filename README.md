# Inventory Stream

A transactional stock ledger with reservations, release, consumption, adjustment reasons, command deduplication and reconciliation across its read model and reservation records. Built for Lord_Sumi's portfolio.

## Test it

On GitHub: **Actions → Tests and demo → Run workflow → main → Run workflow**. Locally, use Python 3.10+ and the standard library:

```bash
python -m unittest discover -s tests -v
python demo.py
```

The temporary synthetic demo receives ten units, reserves three, consumes that reservation and checks that the remaining seven units reconcile.

## Library

```python
from inventory_stream import Inventory

stock = Inventory("stock.db")
stock.create_item("PART-1")
stock.adjust("PART-1", 10, actor="admin", reason="opening stock", command_key="opening-001")
hold = stock.reserve("PART-1", 3, owner="buyer", command_key="reserve-001")
stock.finish(hold["id"], actor="buyer", action="consume", command_key="consume-001")
assert stock.reconcile()["valid"]
```

## State and consistency

Every adjustment, reservation, release and consumption appends a ledger entry in the same SQLite transaction as its state change and command receipt. New items start at zero. Available stock is on-hand minus reserved stock. Negative adjustments cannot reduce on-hand below reserved units. Reservation consumption reduces both on-hand and reserved; release reduces only reserved.

`BEGIN IMMEDIATE` serializes competing commands. Database checks also enforce nonnegative balances and reserved stock no greater than on-hand. A reservation can become either consumed or released once. Only its owner can perform that transition, using a trusted actor identity supplied by the embedding application. This library does not authenticate users or enforce stock-adjustment administrator roles; the embedding service must do so.

Command keys are global idempotency identifiers. Identical retries return the original receipt without another ledger entry. Conflicting key reuse fails. The original receipt can describe an earlier state after later commands; inspect the current stock snapshot for current balances. A rejected command does not consume its key.

`reconcile` uses one database read snapshot and checks ledger totals against item balances and held reservation quantities. It reports differences; it does not automatically repair them. Entries are paginated in append order. The ledger is not cryptographically tamper-evident and cannot prove integrity against an attacker rewriting all representations consistently.

## Verification and limits

Local check on 31 October 2026: **12 tests passed**. Tests cover concurrent reservations without overselling, duplicate requests, conflicting key reuse, protected reserved stock, owner checks, release-versus-consume races, restart persistence, ledger pagination and deliberate read-model damage detection. CI runs the synthetic demo too.

Single-host SQLite domain library with integer quantities. No expiry scheduler, payment processing, multiple warehouses, fractional units, authentication gateway, hosted API or UI. Reservations remain held until explicitly released or consumed. Ledger history and command receipts have no retention policy. Reconciliation queries are not benchmarked for large deployments. Protect the local database and authenticate actors in any application that embeds it.

MIT license; see LICENSE.
