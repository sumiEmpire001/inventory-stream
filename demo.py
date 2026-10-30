import json
import tempfile
from pathlib import Path
from inventory_stream import Inventory

with tempfile.TemporaryDirectory() as directory:
    stock = Inventory(Path(directory) / "stock.db")
    stock.create_item("DEMO-PART")
    stock.adjust("DEMO-PART", 10, actor="demo-admin", reason="synthetic opening stock", command_key="open")
    reservation = stock.reserve("DEMO-PART", 3, owner="demo-owner", command_key="hold")
    stock.finish(reservation["id"], actor="demo-owner", action="consume", command_key="ship")
    print(json.dumps(stock.snapshot("DEMO-PART"), indent=2))
    print(json.dumps(stock.reconcile(), indent=2))
