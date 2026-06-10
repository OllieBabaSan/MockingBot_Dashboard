"""
config.py — Shared data path for MockingBot_Dashboard.
Must match the DATA_DIR in MockingBot/src/utils/config.py
"""
from pathlib import Path

DATA_DIR        = Path(r"C:\Users\user\Downloads\ReplitExport-fabricatorz\Copytradingbot.update")
DB_PATH         = DATA_DIR / "copy_signals.db"
WALLET_STATUS   = DATA_DIR / "wallet_status.csv"
PAPER_POSITIONS = DATA_DIR / "paper_positions.csv"
PAPER_ACCOUNT   = DATA_DIR / "paper_account.csv"
