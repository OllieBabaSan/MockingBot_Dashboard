"""
dashboard.py — MockingBot web dashboard.

Run: python dashboard.py
Opens at http://localhost:5000
Default password: mockingbot
"""

import os
import csv
import sqlite3
import hashlib
import secrets
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, render_template_string, request, redirect, url_for, session
from config import DB_PATH, WALLET_STATUS, PAPER_POSITIONS, PAPER_ACCOUNT

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "mockingbot")
PASSWORD_HASH = hashlib.sha256(DASHBOARD_PASSWORD.encode()).hexdigest()
STARTING_EQUITY = 10000.0


def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def load_account():
    committed = 0.0
    realized_pnl = 0.0
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cur = conn.cursor()

        # Per-wallet budget: divide STARTING_EQUITY equally across active wallets
        active_wallets = load_active_wallet_tiers()
        num_wallets = max(len(active_wallets), 1)
        per_wallet_budget = STARTING_EQUITY / num_wallets

        # Committed capital: sum allocation% × per_wallet_budget for each open ENTRY,
        # then cap each wallet at its per_wallet_budget so heavy wallets don't overflow
        cur.execute("""
            SELECT e.wallet, e.suggested_allocation
            FROM copy_signals e
            WHERE e.signal = 'ENTRY'
              AND NOT EXISTS (
                  SELECT 1 FROM copy_signals x
                  WHERE x.wallet = e.wallet AND x.coin = e.coin
                    AND x.signal = 'EXIT' AND x.timestamp >= e.timestamp
              )
        """)
        wallet_committed = {}
        for wallet, alloc in cur.fetchall():
            if (wallet or "").strip().lower() not in active_wallets:
                continue
            if alloc and alloc.rstrip('%').replace('.', '', 1).isdigit():
                pct = float(alloc.rstrip('%')) / 100
                w = wallet.strip().lower()
                wallet_committed[w] = wallet_committed.get(w, 0.0) + pct * per_wallet_budget

        for wc in wallet_committed.values():
            committed += min(wc, per_wallet_budget)

        # Realized PnL from evaluated EXIT signals
        cur.execute("""
            SELECT SUM(price_change) FROM copy_signals
            WHERE signal = 'EXIT' AND price_change IS NOT NULL
        """)
        row = cur.fetchone()
        if row and row[0] is not None:
            realized_pnl = float(row[0]) * STARTING_EQUITY

        conn.close()
    except Exception as e:
        print(f"load_account error: {e}")

    available_cash = max(0.0, STARTING_EQUITY - committed)
    return {"cash": available_cash, "realized_pnl": realized_pnl}


def load_wallet_counts():
    counts = {"elite": 0, "follow": 0, "candidate": 0, "probation": 0, "rejected": 0}
    try:
        with open(WALLET_STATUS, newline="") as f:
            for row in csv.DictReader(f):
                status = row.get("status", "candidate").strip()
                if status in counts:
                    counts[status] += 1
    except Exception:
        pass
    return counts


def load_active_wallet_tiers() -> set:
    """Return the set of wallet addresses that are elite or follow."""
    active = set()
    try:
        with open(WALLET_STATUS, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("status", "").strip().lower() in ("elite", "follow"):
                    active.add(row.get("wallet", "").strip().lower())
    except Exception:
        pass
    return active


def load_paper_positions():
    active_wallets = load_active_wallet_tiers()
    positions = []
    db_path = DB_PATH.parent / "positions.db"
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT wallet, coin, size, entry_price FROM positions")
        for wallet, coin, size, entry_price in cur.fetchall():
            if wallet.strip().lower() not in active_wallets:
                continue
            size = float(size or 0)
            if size == 0:
                continue
            positions.append({
                "wallet": wallet,
                "coin": coin,
                "size": abs(size),
                "entry_price": entry_price,
                "side": "LONG" if size > 0 else "SHORT",
            })
        conn.close()
    except Exception as e:
        print(f"load_paper_positions error: {e}")
    return positions


# Minimum confidence to display — matches MIN_CONFIDENCE in main.py
DISPLAY_MIN_CONFIDENCE = 7
SIGNALS_POOL = 300
SIGNALS_PER_PAGE = 50


def load_all_signals():
    """Build the 300-entry signal pool. EXIT signals are fetched separately
    so they are never crowded out by high-volume ENTRY/ADD rows."""
    active_wallets = load_active_wallet_tiers()
    rows = []
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cur = conn.cursor()

        # ENTRY/ADD signals for active wallets
        cur.execute("""
            SELECT timestamp, wallet, coin, signal, side,
                   confidence, suggested_allocation, result, price_change
            FROM copy_signals
            WHERE signal != 'EXIT' AND confidence >= ?
            ORDER BY timestamp DESC LIMIT ?
        """, (DISPLAY_MIN_CONFIDENCE, SIGNALS_POOL))
        for row in cur.fetchall():
            ts, wallet, coin, signal, side, conf, alloc, result, price_change = row
            if (wallet or "").strip().lower() not in active_wallets:
                continue
            rows.append(row)

        # All EXIT signals — no wallet filter, no row cap
        cur.execute("""
            SELECT timestamp, wallet, coin, signal, side,
                   confidence, suggested_allocation, result, price_change
            FROM copy_signals
            WHERE signal = 'EXIT'
            ORDER BY timestamp DESC
        """)
        rows.extend(cur.fetchall())
        conn.close()
    except Exception as e:
        print(f"load_all_signals error: {e}")

    rows.sort(key=lambda r: r[0], reverse=True)

    signals = []
    for ts, wallet, coin, signal, side, conf, alloc, result, price_change in rows[:SIGNALS_POOL]:
        if signal == "EXIT":
            if price_change is not None:
                pct = float(price_change) * 100
                result_label = f"{pct:+.2f}%"
                result_cls = "win" if pct > 0 else "loss"
            else:
                result_label = "CLOSED"
                result_cls = "pending"
        else:
            result_label = (result or "pending").upper()
            result_cls = (result or "pending").lower()
        signals.append({
            "time": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S"),
            "wallet": (wallet or "")[:10] + "...",
            "coin": coin,
            "signal": signal,
            "side": side,
            "confidence": conf,
            "allocation": alloc,
            "result_label": result_label,
            "result_cls": result_cls,
        })
    return signals


def load_open_followed():
    """Count unique (wallet, coin) pairs with an open ENTRY signal and no EXIT."""
    active_wallets = load_active_wallet_tiers()
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(DISTINCT e.wallet || ':' || e.coin)
            FROM copy_signals e
            WHERE e.signal = 'ENTRY'
              AND NOT EXISTS (
                  SELECT 1 FROM copy_signals x
                  WHERE x.wallet = e.wallet AND x.coin = e.coin
                    AND x.signal = 'EXIT' AND x.timestamp >= e.timestamp
              )
        """)
        total = cur.fetchone()[0]
        conn.close()
        return total
    except Exception:
        return 0


def load_signal_counts():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM copy_signals")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM copy_signals WHERE checked=1")
        scored = cur.fetchone()[0]
        conn.close()
        return total, scored
    except Exception:
        return 0, 0


TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>MockingBot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0d0f14; --surface: #151820; --border: #1e2330;
    --text: #c8cdd8; --muted: #5a6070;
    --elite: #f0c040; --follow: #60a8f0; --candidate: #8090a8;
    --probation: #e07840; --rejected: #a03030;
    --win: #40c878; --loss: #e05050; --accent: #5090e0;
  }
  body {
    background: var(--bg); color: var(--text);
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 13px; line-height: 1.5;
    padding: 12px; max-width: 680px; margin: 0 auto;
  }
  header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 0 16px; border-bottom: 1px solid var(--border); margin-bottom: 16px;
  }
  .logo { font-size: 18px; font-weight: 700; letter-spacing: 0.05em; color: var(--accent); }
  .logo span { color: var(--elite); }
  .refresh-time { font-size: 11px; color: var(--muted); }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 14px; margin-bottom: 12px; }
  .card-title { font-size: 10px; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
  .pnl-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .pnl-item label { display: block; font-size: 10px; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.08em; }
  .pnl-value { font-size: 22px; font-weight: 700; letter-spacing: -0.02em; }
  .pnl-value.positive { color: var(--win); }
  .pnl-value.negative { color: var(--loss); }
  .pnl-value.neutral { color: var(--text); }
  .pnl-sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .tier-row { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border); }
  .tier-row:last-child { border-bottom: none; }
  .tier-label { display: flex; align-items: center; gap: 8px; }
  .tier-dot { width: 7px; height: 7px; border-radius: 50%; }
  .dot-elite { background: var(--elite); }
  .dot-follow { background: var(--follow); }
  .dot-candidate { background: var(--candidate); }
  .dot-probation { background: var(--probation); }
  .dot-rejected { background: var(--rejected); }
  .tier-count { font-size: 16px; font-weight: 700; }
  .tier-bar-wrap { flex: 1; margin: 0 12px; height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; }
  .tier-bar { height: 100%; border-radius: 2px; }
  .signal-table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .signal-table th { text-align: left; color: var(--muted); font-weight: 500; padding: 0 6px 8px 0; font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
  .signal-table td { padding: 5px 6px 5px 0; border-top: 1px solid var(--border); }
  .sig-entry { color: var(--win); } .sig-exit { color: var(--loss); }
  .sig-add { color: var(--follow); } .sig-reduce { color: var(--elite); }
  .side-long { color: var(--win); } .side-short { color: var(--loss); }
  .result-win { color: var(--win); font-weight: 600; }
  .result-loss { color: var(--loss); font-weight: 600; }
  .result-pending { color: var(--muted); }
  .conf-badge { display: inline-block; padding: 1px 5px; border-radius: 3px; background: var(--border); font-size: 10px; font-weight: 600; }
  .conf-high { background: #1a3a1a; color: var(--win); }
  .conf-mid { background: #1a2a3a; color: var(--follow); }
  @keyframes signal-flash {
    0% { font-size: 1em; color: inherit; }
    20% { font-size: 1.15em; color: var(--elite); }
    100% { font-size: 1em; color: inherit; }
  }
  .signal-new td { animation: signal-flash 1s ease-out; }
  .position-row { display: flex; justify-content: space-between; align-items: center; padding: 7px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
  .position-row:last-child { border-bottom: none; }
  .pos-coin { font-weight: 600; }
  .pos-meta { color: var(--muted); font-size: 10px; margin-top: 2px; }
  footer { text-align: center; color: var(--muted); font-size: 10px; padding: 16px 0 8px; }
  .logout-btn { font-size: 10px; color: var(--muted); text-decoration: none; letter-spacing: 0.08em; }
  .logout-btn:hover { color: var(--text); }
  .pager { display: flex; justify-content: center; align-items: center; gap: 16px; padding: 12px 0 4px; }
  .pager-btn { display: inline-block; color: var(--accent); text-decoration: none; font-size: 20px; line-height: 1; padding: 2px 8px; border-radius: 4px; }
  .pager-btn:hover { background: var(--border); }
  .pager-btn.disabled { color: var(--muted); cursor: default; }
  .pager-info { font-size: 11px; color: var(--muted); letter-spacing: 0.06em; }
</style>
</head>
<body>
<header>
  <div class="logo">MOCKING<span>BOT</span></div>
  <div style="display:flex;align-items:center;gap:16px;">
    <div class="refresh-time">{{ now }} · 30s refresh</div>
    <a href="/logout" class="logout-btn">LOGOUT</a>
  </div>
</header>

<div class="card">
  <div class="card-title">Portfolio</div>
  <div class="pnl-grid">
    <div class="pnl-item">
      <label>Realized PnL</label>
      <div class="pnl-value {{ 'positive' if realized >= 0 else 'negative' }}">
        {{ '+' if realized >= 0 else '' }}${{ '%.2f'|format(realized) }}
      </div>
      <div class="pnl-sub">{{ '+' if realized_pct >= 0 else '' }}{{ '%.2f'|format(realized_pct) }}% return</div>
    </div>
    <div class="pnl-item">
      <label>Cash</label>
      <div class="pnl-value neutral" style="font-size:16px;">${{ '%.0f'|format(cash) }}</div>
      <div class="pnl-sub">of ${{ '%.0f'|format(starting) }} starting</div>
    </div>
    <div class="pnl-item">
      <label>Open Followed</label>
      <div class="pnl-value neutral" style="font-size:16px;">{{ open_followed }}</div>
      <div class="pnl-sub">ENTRY signals without EXIT</div>
    </div>
    <div class="pnl-item">
      <label>Signals Scored</label>
      <div class="pnl-value neutral" style="font-size:16px;">{{ scored_signals }}</div>
      <div class="pnl-sub">of {{ total_signals }} logged</div>
    </div>
  </div>
</div>

<div class="card">
  <div class="card-title">Wallet Tiers</div>
  {% set total_w = counts.elite + counts.follow + counts.candidate + counts.probation %}
  {% for tier, cls in [('elite','dot-elite'),('follow','dot-follow'),('candidate','dot-candidate'),('probation','dot-probation'),('rejected','dot-rejected')] %}
  <div class="tier-row">
    <div class="tier-label">
      <div class="tier-dot {{ cls }}"></div>
      <span style="text-transform:uppercase;font-size:11px;letter-spacing:0.06em;">{{ tier }}</span>
    </div>
    <div class="tier-bar-wrap">
      <div class="tier-bar" style="width:{{ ((counts[tier]/(total_w or 1))*100)|int }}%;background:var(--{{ tier }});"></div>
    </div>
    <div class="tier-count">{{ counts[tier] }}</div>
  </div>
  {% endfor %}
</div>

<div class="card">
  <div class="card-title">Signal History · Page {{ page }} of {{ total_pages }}</div>
  <table class="signal-table">
    <thead><tr><th>Time</th><th>Coin</th><th>Signal</th><th>Side</th><th>Conf</th><th>Result</th></tr></thead>
    <tbody>
    {% for s in signals %}
    <tr class="{{ 'signal-new' if loop.index <= 3 and page == 1 else '' }}">
      <td style="color:var(--muted);">{{ s.time }}</td>
      <td style="font-weight:600;">{{ s.coin }}</td>
      <td class="sig-{{ s.signal|lower }}">{{ s.signal }}</td>
      <td class="side-{{ s.side|lower }}">{{ s.side }}</td>
      <td><span class="conf-badge {{ 'conf-high' if s.confidence >= 8 else 'conf-mid' if s.confidence >= 6 else '' }}">{{ s.confidence }}</span></td>
      <td class="result-{{ s.result_cls }}">{{ s.result_label }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  <div class="pager">
    {% if page > 1 %}
    <a href="/?page={{ page - 1 }}" class="pager-btn">&#8249;</a>
    {% else %}
    <span class="pager-btn disabled">&#8249;</span>
    {% endif %}
    <span class="pager-info">{{ page }} / {{ total_pages }}</span>
    {% if page < total_pages %}
    <a href="/?page={{ page + 1 }}" class="pager-btn">&#8250;</a>
    {% else %}
    <span class="pager-btn disabled">&#8250;</span>
    {% endif %}
  </div>
</div>

{% if positions %}
<div class="card">
  <div class="card-title">Open Positions ({{ positions|length }})</div>
  {% for p in positions %}
  <div class="position-row">
    <div>
      <div class="pos-coin">{{ p.coin }} <span class="side-{{ p.side|lower }}" style="font-size:11px;">{{ p.side }}</span></div>
      <div class="pos-meta">entry {{ p.entry_price }}</div>
    </div>
    <div style="text-align:right;font-size:10px;color:var(--muted);">{{ p.wallet[:10] }}...</div>
  </div>
  {% endfor %}
</div>
{% endif %}

<footer>MockingBot · {{ now }}</footer>
</body>
</html>
"""

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MockingBot · Login</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d0f14; color: #c8cdd8; font-family: 'SF Mono','Fira Code',monospace; display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 24px; }
  .card { background: #151820; border: 1px solid #1e2330; border-radius: 8px; padding: 32px; width: 100%; max-width: 320px; }
  .logo { font-size: 20px; font-weight: 700; color: #5090e0; margin-bottom: 24px; letter-spacing: 0.05em; }
  .logo span { color: #f0c040; }
  input[type=password] { width: 100%; background: #0d0f14; border: 1px solid #1e2330; border-radius: 4px; color: #c8cdd8; font-family: inherit; font-size: 14px; padding: 10px 12px; margin-bottom: 12px; outline: none; }
  input[type=password]:focus { border-color: #5090e0; }
  button { width: 100%; background: #5090e0; border: none; border-radius: 4px; color: #fff; font-family: inherit; font-size: 13px; font-weight: 600; letter-spacing: 0.08em; padding: 10px; cursor: pointer; text-transform: uppercase; }
  button:hover { background: #4080d0; }
  .error { color: #e05050; font-size: 11px; margin-bottom: 12px; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">MOCKING<span>BOT</span></div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Enter</button>
  </form>
</div>
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hashlib.sha256(pw.encode()).hexdigest() == PASSWORD_HASH:
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Incorrect password"
    return render_template_string(LOGIN_TEMPLATE, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@require_login
def index():
    page = max(1, request.args.get("page", 1, type=int))

    account = load_account()
    positions = load_paper_positions()
    all_signals = load_all_signals()
    counts = load_wallet_counts()
    total_signals, scored_signals = load_signal_counts()
    open_followed = load_open_followed()

    total_pages = max(1, (len(all_signals) + SIGNALS_PER_PAGE - 1) // SIGNALS_PER_PAGE)
    page = min(page, total_pages)
    start = (page - 1) * SIGNALS_PER_PAGE
    signals = all_signals[start:start + SIGNALS_PER_PAGE]

    realized = account.get("realized_pnl", 0.0)
    cash = account.get("cash", STARTING_EQUITY)
    realized_pct = (realized / STARTING_EQUITY) * 100
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    return render_template_string(
        TEMPLATE,
        realized=realized, realized_pct=realized_pct,
        cash=cash, starting=STARTING_EQUITY,
        positions=positions, signals=signals,
        counts=counts, total_signals=total_signals,
        scored_signals=scored_signals, now=now,
        page=page, total_pages=total_pages,
        open_followed=open_followed,
    )


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", 5000))
    print(f"MockingBot dashboard at http://localhost:{port}")
    print(f"Password: {DASHBOARD_PASSWORD}")
    print(f"Reading data from: {DB_PATH.parent}")
    app.run(host="0.0.0.0", port=port, debug=False)
