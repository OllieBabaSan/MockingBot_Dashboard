import csv
from config import PAPER_ACCOUNT

STARTING_CASH = 10000.0


def load_account():
    try:
        with open(PAPER_ACCOUNT, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                return {
                    "cash": float(row.get("cash", STARTING_CASH)),
                    "realized_pnl": float(row.get("realized_pnl", 0.0))
                }
    except Exception:
        pass
    return {"cash": STARTING_CASH, "realized_pnl": 0.0}
