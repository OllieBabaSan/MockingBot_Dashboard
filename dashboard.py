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
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, render_template_string, request, redirect, url_for, session, send_file, jsonify
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


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_account():
    """Read cash and realized PnL from paper_account.csv (written by paper_account.py)."""
    try:
        with open(PAPER_ACCOUNT, newline="") as f:
            for row in csv.DictReader(f):
                cash = float(row.get("cash") or STARTING_EQUITY)
                realized_pnl = float(row.get("realized_pnl") or 0.0)
                return {"cash": cash, "realized_pnl": realized_pnl}
    except Exception:
        pass
    return {"cash": STARTING_EQUITY, "realized_pnl": 0.0}


def load_wallet_counts():
    counts = {"elite": 0, "follow": 0, "candidate": 0, "probation": 0, "rejected": 0}
    try:
        with open(WALLET_STATUS, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                status = row.get("status", "candidate").strip()
                if status in counts:
                    counts[status] += 1
    except Exception:
        pass
    return counts


def load_active_wallet_tiers() -> set:
    """Return wallet addresses that are elite or follow."""
    active = set()
    try:
        with open(WALLET_STATUS, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status", "").strip().lower() in ("elite", "follow"):
                    active.add(row.get("wallet", "").strip().lower())
    except Exception:
        pass
    return active


def load_paper_positions():
    """
    Load paper positions from paper_positions.csv.
    Schema: wallet, coin, side, size, entry_price, pnl, cost_basis
    Only shows positions for elite/follow wallets.
    """
    active_wallets = load_active_wallet_tiers()
    positions = []
    try:
        with open(PAPER_POSITIONS, newline="") as f:
            for row in csv.DictReader(f):
                wallet = (row.get("wallet") or "").strip().lower()
                if wallet not in active_wallets:
                    continue
                size = float(row.get("size") or 0)
                if size <= 0:
                    continue
                positions.append({
                    "wallet": wallet,
                    "coin": row.get("coin", ""),
                    "side": row.get("side", "LONG"),
                    "size": size,
                    "entry_price": float(row.get("entry_price") or 0),
                    "pnl": float(row.get("pnl") or 0),
                })
    except Exception as e:
        print(f"load_paper_positions error: {e}")
    return positions


SIGNALS_POOL = 300
SIGNALS_PER_PAGE = 50


def load_all_signals():
    """
    Build the 300-entry signal pool.
    All signal types filtered to elite/follow wallets only — no confidence floor.
    """
    active_wallets = load_active_wallet_tiers()
    if not active_wallets:
        return []

    placeholders = ",".join("?" * len(active_wallets))
    rows = []
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        cur = conn.cursor()

        cur.execute(f"""
            SELECT timestamp, wallet, coin, signal, side,
                   confidence, suggested_allocation, result, price_change
            FROM copy_signals
            WHERE wallet IN ({placeholders})
            ORDER BY timestamp DESC LIMIT ?
        """, (*active_wallets, SIGNALS_POOL))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        print(f"load_all_signals error: {e}")

    signals = []
    for ts, wallet, coin, signal, side, conf, alloc, result, price_change in rows:
        if signal == "EXIT":
            if price_change is not None:
                pct = float(price_change) * 100
                result_label = f"{pct:+.2f}%"
                result_cls = "win" if pct > 0 else "loss"
            elif result in ("WIN", "LOSS"):
                result_label = result
                result_cls = result.lower()
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


def load_open_position_count():
    """Count open paper positions for active wallets."""
    active_wallets = load_active_wallet_tiers()
    count = 0
    try:
        with open(PAPER_POSITIONS, newline="") as f:
            for row in csv.DictReader(f):
                wallet = (row.get("wallet") or "").strip().lower()
                if wallet in active_wallets and float(row.get("size") or 0) > 0:
                    count += 1
    except Exception:
        pass
    return count


def load_signal_counts():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM copy_signals")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM copy_signals WHERE checked=1")
        scored = cur.fetchone()[0]
        conn.close()
        return total, scored
    except Exception:
        return 0, 0


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
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
    display: flex; flex-direction: column; align-items: center;
    padding: 12px 0 16px; border-bottom: 1px solid var(--border); margin-bottom: 16px;
    gap: 8px;
  }
  .header-meta { display: flex; justify-content: space-between; width: 100%; align-items: center; }
  .logo { display: flex; align-items: center; gap: 8px; font-size: 21px; font-weight: 700; letter-spacing: 0.05em; color: #7B3FB0; }
  .logo img { height: 76px; width: auto; }
  .logo span { color: #C80000; }
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
  .sig-entry { color: var(--win); } .sig-close { color: var(--loss); }
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
  .pos-left { flex: 1; }
  .pos-coin { font-weight: 600; }
  .pos-meta { color: var(--muted); font-size: 10px; margin-top: 2px; }
  .pos-pnl { font-size: 11px; font-weight: 600; margin-top: 2px; }
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
  <div class="logo"><img src="/logo.png" alt="MockingBot">MOCKING<span>BOT</span></div>
  <div class="header-meta">
    <div class="refresh-time" id="refresh-ts">{{ now }} · 30s refresh</div>
    <a href="/logout" class="logout-btn">LOGOUT</a>
  </div>
</header>

<div class="card">
  <div class="card-title">Portfolio</div>
  <div class="pnl-grid">
    <div class="pnl-item">
      <label>Realized PnL</label>
      <div id="pnl-realized" class="pnl-value {{ 'positive' if realized >= 0 else 'negative' }}">
        {{ '+' if realized >= 0 else '' }}${{ '%.2f'|format(realized) }}
      </div>
      <div id="pnl-realized-pct" class="pnl-sub">{{ '+' if realized_pct >= 0 else '' }}{{ '%.2f'|format(realized_pct) }}% return</div>
    </div>
    <div class="pnl-item">
      <label>Account Value</label>
      <div id="pnl-account" class="pnl-value {{ 'positive' if (starting + realized) >= starting else 'negative' }}" style="font-size:16px;">${{ '%.0f'|format(starting + realized) }}</div>
      <div class="pnl-sub">started ${{ '%.0f'|format(starting) }}</div>
    </div>
    <div class="pnl-item">
      <label>Open Positions</label>
      <div id="pnl-positions" class="pnl-value neutral" style="font-size:16px;">{{ open_count }}</div>
    </div>
    <div class="pnl-item">
      <label>Signals Scored</label>
      <div id="pnl-scored" class="pnl-value neutral" style="font-size:16px;">{{ scored_signals }}</div>
      <div id="pnl-total" class="pnl-sub">of {{ total_signals }} logged</div>
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
    <tbody id="signals-body">
    {% for s in signals %}
    <tr class="{{ 'signal-new' if loop.index <= 3 and page == 1 else '' }}">
      <td style="color:var(--muted);">{{ s.time }}</td>
      <td style="font-weight:600;">{{ s.coin }}</td>
      <td class="sig-{{ 'close' if s.signal == 'EXIT' else s.signal|lower }}">{{ 'CLOSE' if s.signal == 'EXIT' else s.signal }}</td>
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
    <div class="pos-left">
      <div class="pos-coin">{{ p.coin }} <span class="side-{{ p.side|lower }}" style="font-size:11px;">{{ p.side }}</span></div>
      <div class="pos-meta">entry {{ p.entry_price }}</div>
      {% if p.pnl != 0 %}
      <div class="pos-pnl {{ 'side-long' if p.pnl >= 0 else 'side-short' }}">{{ '+' if p.pnl >= 0 else '' }}{{ '%.2f'|format(p.pnl) }} PnL</div>
      {% endif %}
    </div>
    <div style="text-align:right;font-size:10px;color:var(--muted);">{{ p.wallet[:10] }}...</div>
  </div>
  {% endfor %}
</div>
{% endif %}

<footer>MockingBot · {{ now }}</footer>
<script>
(function() {
  var currentPage = {{ page }};

  function confClass(c) {
    return c >= 8 ? 'conf-high' : c >= 6 ? 'conf-mid' : '';
  }

  function renderSignals(signals) {
    var tbody = document.getElementById('signals-body');
    if (!tbody) return;
    tbody.innerHTML = signals.map(function(s, i) {
      var sigCls = s.signal === 'EXIT' ? 'sig-close' : 'sig-' + s.signal.toLowerCase();
      var sigLabel = s.signal === 'EXIT' ? 'CLOSE' : s.signal;
      var rowCls = (i < 3 && currentPage === 1) ? 'signal-new' : '';
      return '<tr class="' + rowCls + '">' +
        '<td style="color:var(--muted);">' + s.time + '</td>' +
        '<td style="font-weight:600;">' + s.coin + '</td>' +
        '<td class="' + sigCls + '">' + sigLabel + '</td>' +
        '<td class="side-' + s.side.toLowerCase() + '">' + s.side + '</td>' +
        '<td><span class="conf-badge ' + confClass(s.confidence) + '">' + s.confidence + '</span></td>' +
        '<td class="result-' + s.result_cls + '">' + s.result_label + '</td>' +
        '</tr>';
    }).join('');
  }

  function setText(id, val) {
    var el = document.getElementById(id);
    if (el) el.textContent = val;
  }

  function setClass(id, cls) {
    var el = document.getElementById(id);
    if (el) { el.className = el.className.replace(/\b(positive|negative|neutral)\b/g, '').trim() + ' ' + cls; }
  }

  function refreshAccount() {
    fetch('/api/account', {credentials: 'same-origin'})
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(d) {
        if (!d) return;
        var sign = d.realized >= 0 ? '+' : '';
        setText('pnl-realized', sign + '$' + d.realized.toFixed(2));
        setClass('pnl-realized', d.realized >= 0 ? 'positive' : 'negative');
        setText('pnl-realized-pct', (d.realized_pct >= 0 ? '+' : '') + d.realized_pct.toFixed(2) + '% return');
        setText('pnl-account', '$' + Math.round(d.account_value));
        setClass('pnl-account', d.account_value >= {{ starting }} ? 'positive' : 'negative');
        setText('pnl-positions', d.open_count);
        setText('pnl-scored', d.scored_signals);
        setText('pnl-total', 'of ' + d.total_signals + ' logged');
      })
      .catch(function() {});
  }

  function refreshSignals() {
    fetch('/api/signals?page=' + currentPage, {credentials: 'same-origin'})
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(data) {
        if (!data) return;
        renderSignals(data.signals);
        var ts = document.getElementById('refresh-ts');
        if (ts) {
          var hms = new Date().toUTCString().match(/(\\d{2}:\\d{2}:\\d{2})/);
          ts.textContent = (hms ? hms[1] : '') + ' UTC · 30s refresh';
        }
      })
      .catch(function() {});
  }

  function refresh() { refreshAccount(); refreshSignals(); }

  setInterval(refresh, 30000);
})();
</script>
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
  .logo { font-size: 20px; font-weight: 700; color: #7B3FB0; margin-bottom: 24px; letter-spacing: 0.05em; }
  .logo span { color: #C80000; }
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/account")
@require_login
def api_account():
    account = load_account()
    total_signals, scored_signals = load_signal_counts()
    open_count = load_open_position_count()
    realized = account.get("realized_pnl", 0.0)
    realized_pct = (realized / STARTING_EQUITY) * 100
    return jsonify({
        "realized": realized,
        "realized_pct": realized_pct,
        "account_value": STARTING_EQUITY + realized,
        "open_count": open_count,
        "scored_signals": scored_signals,
        "total_signals": total_signals,
    })


@app.route("/api/signals")
@require_login
def api_signals():
    page = max(1, request.args.get("page", 1, type=int))
    all_signals = load_all_signals()
    total_pages = max(1, (len(all_signals) + SIGNALS_PER_PAGE - 1) // SIGNALS_PER_PAGE)
    page = min(page, total_pages)
    start = (page - 1) * SIGNALS_PER_PAGE
    return jsonify({
        "signals": all_signals[start:start + SIGNALS_PER_PAGE],
        "page": page,
        "total_pages": total_pages,
    })


@app.route("/logo.png")
def logo():
    return send_file(Path(__file__).parent / "logo.png", mimetype="image/png")


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
    open_count = load_open_position_count()

    total_pages = max(1, (len(all_signals) + SIGNALS_PER_PAGE - 1) // SIGNALS_PER_PAGE)
    page = min(page, total_pages)
    start = (page - 1) * SIGNALS_PER_PAGE
    signals = all_signals[start:start + SIGNALS_PER_PAGE]

    realized = account.get("realized_pnl", 0.0)
    realized_pct = (realized / STARTING_EQUITY) * 100
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    return render_template_string(
        TEMPLATE,
        realized=realized, realized_pct=realized_pct,
        starting=STARTING_EQUITY,
        positions=positions, signals=signals,
        counts=counts, total_signals=total_signals,
        scored_signals=scored_signals, now=now,
        page=page, total_pages=total_pages,
        open_count=open_count,
    )


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", 5000))
    print(f"MockingBot dashboard at http://localhost:{port}")
    print(f"Password: {DASHBOARD_PASSWORD}")
    print(f"Reading data from: {DB_PATH.parent}")
    app.run(host="0.0.0.0", port=port, debug=False)
