MockingBot Dashboard
====================

Setup:
  python -m venv venv
  .\venv\Scripts\activate
  pip install -r requirements.txt

Run:
  python dashboard.py

Then open http://localhost:5000 in your browser.
Default password: mockingbot

The dashboard reads these files from its own folder:
  copy_signals.db     (copy from MockingBot after each run)
  wallet_status.csv   (copy from MockingBot after each run)
  paper_positions.csv (copy from MockingBot after each run)
  paper_account.csv   (copy from MockingBot after each run)

To change password:
  Set DASHBOARD_PASSWORD environment variable, or edit dashboard.py line ~20
