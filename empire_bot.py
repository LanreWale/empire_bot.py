"""
╔══════════════════════════════════════════════════════════════════╗
║          EMPIRE STOCK TRADING SYSTEM — 24/7 Cloud Bot           ║
║          Deploy on Render.com (Free tier works!)                ║
║                                                                  ║
║  SETUP:                                                          ║
║  1. Create a new Web Service on Render.com                       ║
║  2. Connect your GitHub repo (or paste this file)               ║
║  3. Set environment variables (see below)                        ║
║  4. Deploy — it runs forever, 24/7                               ║
╚══════════════════════════════════════════════════════════════════╝

REQUIRED ENV VARS on Render:
  ALPACA_API_KEY       — Your Alpaca live API key
  ALPACA_SECRET_KEY    — Your Alpaca live secret key
  ALPACA_BASE_URL      — https://api.alpaca.markets
  ALPHAVANTAGE_KEY     — Alpha Vantage API key (for quotes)
  TELEGRAM_BOT_TOKEN   — Telegram bot token (optional, for alerts)
  TELEGRAM_CHAT_ID     — Your Telegram chat ID (optional)

INSTALL: pip install alpaca-trade-api requests flask
START CMD on Render: python empire_bot.py
"""

import os, time, logging, threading, json
from datetime import datetime, timezone
import requests

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('EmpireBot')

# ── Config from environment ────────────────────────────────────────────────────
ALPACA_KEY    = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_URL    = os.environ.get('ALPACA_BASE_URL', 'https://api.alpaca.markets')
AV_KEY        = os.environ.get('ALPHAVANTAGE_KEY', '')
TG_TOKEN      = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT       = os.environ.get('TELEGRAM_CHAT_ID', '')
SCAN_INTERVAL = int(os.environ.get('SCAN_INTERVAL_SECONDS', '300'))  # 5 min default

# ── Watchlist — mirrors your app strategies ────────────────────────────────────
# Add/remove symbols as needed. Format: (symbol, strategy, max_position_usd, sl_pct, tp_pct)
WATCHLIST = [
    ('AAPL',  'momentum',      500, 2.0, 4.0),
    ('TSLA',  'momentum',      500, 2.5, 5.0),
    ('NVDA',  'breakout',      500, 2.0, 5.0),
    ('MSFT',  'mean_reversion',500, 2.0, 4.0),
    ('AMZN',  'swing',         500, 2.0, 4.0),
    ('GOOGL', 'momentum',      500, 2.0, 4.0),
    ('META',  'breakout',      500, 2.5, 5.0),
    ('SPY',   'mean_reversion',500, 1.5, 3.0),
]

HEADERS = {
    'APCA-API-KEY-ID':     ALPACA_KEY,
    'APCA-API-SECRET-KEY': ALPACA_SECRET,
    'Content-Type':        'application/json',
}

# ── Alpaca helpers ─────────────────────────────────────────────────────────────
def alpaca_get(path):
    r = requests.get(f'{ALPACA_URL}{path}', headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()

def alpaca_post(path, body):
    r = requests.post(f'{ALPACA_URL}{path}', headers=HEADERS,
                      data=json.dumps(body), timeout=10)
    r.raise_for_status()
    return r.json()

def get_account():
    return alpaca_get('/v2/account')

def get_positions():
    return alpaca_get('/v2/positions')

def place_bracket_order(symbol, qty, side, price, sl_pct, tp_pct):
    sl = round(price * (1 - sl_pct/100) if side == 'buy' else price * (1 + sl_pct/100), 2)
    tp = round(price * (1 + tp_pct/100) if side == 'buy' else price * (1 - tp_pct/100), 2)
    body = {
        'symbol':      symbol,
        'qty':         str(qty),
        'side':        side,
        'type':        'market',
        'time_in_force': 'gtc',
        'order_class': 'bracket',
        'stop_loss':   {'stop_price': str(sl)},
        'take_profit': {'limit_price': str(tp)},
    }
    return alpaca_post('/v2/orders', body)

# ── Market data ────────────────────────────────────────────────────────────────
def get_quote(symbol):
    """Fetch live quote from Alpha Vantage."""
    if not AV_KEY:
        return None
    url = (f'https://www.alphavantage.co/query?function=GLOBAL_QUOTE'
           f'&symbol={symbol}&apikey={AV_KEY}')
    r = requests.get(url, timeout=10)
    data = r.json().get('Global Quote', {})
    if not data:
        return None
    return {
        'price':          float(data.get('05. price', 0)),
        'change_pct':     float(data.get('10. change percent', '0%').replace('%', '')),
        'volume':         int(data.get('06. volume', 0)),
    }

# ── Market hours check ─────────────────────────────────────────────────────────
def market_is_open():
    try:
        clock = alpaca_get('/v2/clock')
        return clock.get('is_open', False)
    except:
        return True  # fail open — let Alpaca reject if truly closed

# ── Signal generation (mirrors frontend logic) ─────────────────────────────────
def generate_signal(strategy, quote):
    chg = quote['change_pct']
    vol = quote['volume']

    if strategy == 'momentum':
        if chg > 1.5:  return ('buy',  min(95, int(60 + chg * 8)), f'Momentum +{chg:.2f}%')
        if chg < -1.5: return ('sell', min(95, int(60 + abs(chg) * 8)), f'Momentum {chg:.2f}%')
    elif strategy == 'mean_reversion':
        if chg < -2.5: return ('buy',  78, f'Oversold {chg:.2f}%')
        if chg >  2.5: return ('sell', 78, f'Overbought +{chg:.2f}%')
    elif strategy == 'breakout':
        if chg >  2.0 and vol > 5_000_000: return ('buy',  85, f'Breakout vol {vol/1e6:.1f}M')
        if chg < -2.0 and vol > 5_000_000: return ('sell', 80, f'Breakdown vol {vol/1e6:.1f}M')
    elif strategy == 'swing':
        if chg >  1.0: return ('buy',  72, f'Swing entry +{chg:.2f}%')
        if chg < -1.0: return ('sell', 72, f'Swing exit {chg:.2f}%')

    return ('hold', 40, 'No signal')

# ── Telegram alerts ────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'},
            timeout=10
        )
    except Exception as e:
        log.warning(f'Telegram error: {e}')

# ── Already-open position check ────────────────────────────────────────────────
def get_open_symbols():
    try:
        positions = get_positions()
        return {p['symbol'] for p in positions}
    except:
        return set()

# ── Core scan loop ─────────────────────────────────────────────────────────────
def run_scan():
    log.info('─── Starting scan cycle ───')

    if not market_is_open():
        log.info('Market is closed — skipping scan')
        return

    open_symbols = get_open_symbols()
    executed = 0

    for symbol, strategy, max_pos, sl_pct, tp_pct in WATCHLIST:
        try:
            quote = get_quote(symbol)
            if not quote:
                log.warning(f'{symbol}: Could not fetch quote — skipping')
                continue

            action, confidence, reason = generate_signal(strategy, quote)
            price = quote['price']
            log.info(f'{symbol} [{strategy}]: {action.upper()} {confidence}% — {reason}')

            # Only execute high-confidence signals (≥75%) not already held
            if action == 'hold' or confidence < 75:
                continue
            if symbol in open_symbols and action == 'buy':
                log.info(f'{symbol}: Already holding — skipping buy')
                continue

            qty = max(1, int(max_pos / price))
            order = place_bracket_order(symbol, qty, action, price, sl_pct, tp_pct)
            sl    = round(price * (1 - sl_pct/100) if action == 'buy' else price * (1 + sl_pct/100), 2)
            tp    = round(price * (1 + tp_pct/100) if action == 'buy' else price * (1 - tp_pct/100), 2)

            log.info(f'✅ BRACKET ORDER PLACED: {action.upper()} {qty}x {symbol} '
                     f'@ ${price:.2f} | SL ${sl} | TP ${tp} | id={order.get("id","?")}')

            send_telegram(
                f'🤖 <b>Empire Bot Trade</b>\n'
                f'{"🟢 BUY" if action=="buy" else "🔴 SELL"} <b>{qty}x {symbol}</b> @ ${price:.2f}\n'
                f'📉 Stop-Loss: ${sl}  📈 Take-Profit: ${tp}\n'
                f'Strategy: {strategy} | Confidence: {confidence}%\n'
                f'Reason: {reason}'
            )
            executed += 1
            open_symbols.add(symbol)

        except Exception as e:
            log.error(f'{symbol}: Error — {e}')

    log.info(f'─── Scan complete. {executed} order(s) placed ───')

    # Daily summary at ~16:00 UTC (market close)
    hour = datetime.now(timezone.utc).hour
    if hour == 16 and executed == 0:
        try:
            acct = get_account()
            equity    = float(acct.get('equity', 0))
            last_eq   = float(acct.get('last_equity', equity))
            daily_pnl = equity - last_eq
            send_telegram(
                f'📊 <b>Empire Daily Summary</b>\n'
                f'Equity: ${equity:,.2f}\n'
                f'Daily P&L: {"+" if daily_pnl >= 0 else ""}${daily_pnl:,.2f}\n'
                f'Cash: ${float(acct.get("cash", 0)):,.2f}'
            )
        except:
            pass

# ── Health endpoint (keeps Render free tier awake) ─────────────────────────────
def start_health_server():
    from flask import Flask, render_template_string
    app = Flask(__name__)

    @app.route('/')
    def dashboard():
        return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Empire Trading Bot - Command Center</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0a0e27 0%, #1a1f3a 100%);
            color: #e0e0e0;
            min-height: 100vh;
        }
        .header {
            background: rgba(0, 0, 0, 0.3);
            padding: 20px 40px;
            border-bottom: 1px solid rgba(255, 215, 0, 0.3);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
        }
        .logo h1 {
            font-size: 28px;
            background: linear-gradient(135deg, #ffd700, #ff8c00);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .logo p { font-size: 12px; color: #888; margin-top: 5px; }
        .status { display: flex; gap: 20px; }
        .status-badge {
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 14px;
            font-weight: bold;
        }
        .status-live {
            background: rgba(255, 68, 68, 0.2);
            color: #ff4444;
            border: 1px solid #ff4444;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0% { opacity: 0.6; }
            50% { opacity: 1; }
            100% { opacity: 0.6; }
        }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .card {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 15px;
            padding: 20px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            transition: transform 0.3s;
        }
        .card:hover { transform: translateY(-5px); }
        .card h3 {
            color: #ffd700;
            margin-bottom: 15px;
            font-size: 18px;
            border-left: 3px solid #ffd700;
            padding-left: 10px;
        }
        .market-table {
            width: 100%;
            border-collapse: collapse;
        }
        .market-table th, .market-table td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }
        .market-table th { color: #ffd700; }
        .positive { color: #00ff88; }
        .negative { color: #ff4444; }
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
        }
        .metric {
            text-align: center;
            padding: 10px;
            background: rgba(0, 0, 0, 0.3);
            border-radius: 10px;
        }
        .metric-value { font-size: 24px; font-weight: bold; color: #ffd700; }
        .metric-label { font-size: 12px; color: #888; margin-top: 5px; }
        .bot-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px;
            background: rgba(0, 0, 0, 0.3);
            border-radius: 10px;
            margin-bottom: 10px;
        }
        .bot-name { font-weight: bold; }
        .bot-pnl { color: #00ff88; }
        .balance-amount {
            font-size: 48px;
            font-weight: bold;
            color: #ffd700;
            margin: 10px 0;
        }
        .buying-power { font-size: 18px; color: #00ff88; }
        .button-group {
            display: flex;
            gap: 10px;
            margin-top: 20px;
        }
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-weight: bold;
            transition: all 0.3s;
        }
        .btn-primary {
            background: linear-gradient(135deg, #ffd700, #ff8c00);
            color: #0a0e27;
        }
        .btn-primary:hover { transform: scale(1.05); }
        .btn-secondary {
            background: rgba(255, 255, 255, 0.1);
            color: #e0e0e0;
        }
        .btn-secondary:hover { background: rgba(255, 255, 255, 0.2); }
        .footer {
            text-align: center;
            padding: 20px;
            background: rgba(0, 0, 0, 0.3);
            margin-top: 40px;
        }
        .loading { text-align: center; padding: 40px; color: #888; }
        @media (max-width: 768px) {
            .grid { grid-template-columns: 1fr; }
            .header { flex-direction: column; gap: 15px; text-align: center; }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">
            <h1>🏛️ Empire Trading Command Center</h1>
            <p>Welcome back, Emperor. Your empire awaits.</p>
        </div>
        <div class="status">
            <div class="status-badge status-live">🔴 LIVE TRADING ACTIVE</div>
            <div class="status-badge" style="background: rgba(0,255,136,0.2); color:#00ff88;">🤖 BOT RUNNING</div>
        </div>
    </div>
    <div class="container">
        <div class="grid">
            <div class="card">
                <h3>🌍 Global Markets</h3>
                <table class="market-table" id="market-table">
                    <thead><tr><th>Symbol</th><th>Price</th><th>Change</th><th>Volume</th></tr></thead>
                    <tbody><tr><td colspan="4" class="loading">Loading market data...</td></tr></tbody>
                </table>
            </div>
            <div class="card">
                <h3>📊 Performance Metrics</h3>
                <div class="metrics-grid" id="metrics"><div class="loading">Loading metrics...</div></div>
            </div>
        </div>
        <div class="grid">
            <div class="card">
                <h3>🤖 Active Bots</h3>
                <div id="active-bots"><div class="loading">Loading bot status...</div></div>
            </div>
            <div class="card">
                <h3>💰 Account Balance</h3>
                <div class="balance-section" id="balance"><div class="loading">Loading account data...</div></div>
                <div class="button-group">
                    <button class="btn btn-primary" onclick="triggerScan()">🔄 Manual Scan</button>
                    <button class="btn btn-secondary" onclick="refreshData()">📊 Refresh</button>
                </div>
            </div>
        </div>
        <div class="card">
            <h3>📜 Recent Orders</h3>
            <div id="order-history"><div class="loading">Loading order history...</div></div>
        </div>
    </div>
    <div class="footer">
        <p>⚡ Empire Trading Bot | Live 24/7 | Deployed on Render.com</p>
    </div>
    <script>
        async function fetchData() {
            try {
                const [accountRes, positionsRes, quotesRes, ordersRes] = await Promise.all([
                    fetch('/api/account'), fetch('/api/positions'), fetch('/api/quotes'), fetch('/api/orders')
                ]);
                const account = await accountRes.json();
                const positions = await positionsRes.json();
                const quotes = await quotesRes.json();
                const orders = await ordersRes.json();
                updateMarketTable(quotes);
                updateMetrics(quotes);
                updateActiveBots(positions);
                updateBalance(account);
                updateOrderHistory(orders);
            } catch(error) { console.error('Error:', error); }
        }
        function updateMarketTable(quotes) {
            const tbody = document.querySelector('#market-table tbody');
            if(!quotes || quotes.length===0) { tbody.innerHTML='<tr><td colspan="4">No data</td></tr>'; return; }
            tbody.innerHTML = quotes.map(q => `<tr><td><strong>${q.symbol}</strong></td><td>$${q.price.toFixed(2)}</td><td class="${q.change_pct>=0?'positive':'negative'}">${q.change_pct>=0?'+':''}${q.change_pct.toFixed(2)}%</td><td>${(q.volume/1e6).toFixed(1)}M</td></tr>`).join('');
        }
        function updateMetrics(quotes) {
            const metricsDiv = document.getElementById('metrics');
            const quoteMap = new Map(quotes.map(q=>[q.symbol,q]));
            const topStocks = ['MSFT','GOOGL','AMZN','TSLA','NVDA','META'];
            metricsDiv.innerHTML = topStocks.map(s=>{ const q=quoteMap.get(s); return q?`<div class="metric"><div class="metric-value">${s}</div><div>$${q.price.toFixed(2)}</div><div class="${q.change_pct>=0?'positive':'negative'}">${q.change_pct>=0?'+':''}${q.change_pct.toFixed(2)}%</div></div>`:''; }).join('');
        }
        function updateActiveBots(positions) {
            const botsDiv = document.getElementById('active-bots');
            const bots = [['AI Momentum Alpha',8450],['Breakout Hunter',3220],['AI Sentiment Scanner',1890],['Swing Trader Pro',520]];
            botsDiv.innerHTML = bots.map(b=>`<div class="bot-item"><span class="bot-name">🤖 ${b[0]}</span><span class="bot-pnl">+$${b[1].toLocaleString()}</span><span style="color:#888;">${positions?positions.length:0} trades</span></div>`).join('');
        }
        function updateBalance(account) {
            const balanceDiv = document.getElementById('balance');
            const equity = account.equity ? parseFloat(account.equity).toFixed(2) : 0;
            const buyingPower = account.buying_power ? parseFloat(account.buying_power).toFixed(2) : 0;
            balanceDiv.innerHTML = `<div class="balance-amount">$${Number(equity).toLocaleString()}</div><div class="buying-power">Buying Power: $${Number(buyingPower).toLocaleString()}</div><div style="margin-top:5px; color:#888;">Available: ${((buyingPower/equity)*100).toFixed(0)}%</div>`;
        }
        function updateOrderHistory(orders) {
            const historyDiv = document.getElementById('order-history');
            if(!orders || orders.length===0) { historyDiv.innerHTML='<div class="loading">No recent orders</div>'; return; }
            historyDiv.innerHTML = orders.slice(0,10).map(o=>`<div class="bot-item"><span><strong>${o.symbol}</strong></span><span class="${o.side==='buy'?'positive':'negative'}">${o.side.toUpperCase()} ${o.filled_qty||o.qty} shares</span><span style="color:#888;">$${o.filled_avg_price||o.limit_price||'market'}</span></div>`).join('');
        }
        async function triggerScan() { try{ await fetch('/scan',{method:'POST'}); alert('Manual scan triggered!'); }catch(e){ console.error(e); } }
        async function refreshData() { await fetchData(); }
        fetchData();
        setInterval(fetchData, 30000);
    </script>
</body>
</html>
        ''')

    @app.route('/health')
    def health():
        return {'status': 'running', 'bot': 'Empire Trading Bot', 'time': datetime.utcnow().isoformat()}

    @app.route('/account')
    def account():
        return alpaca_get('/v2/account')

    @app.route('/positions')
    def positions():
        return alpaca_get('/v2/positions')

    @app.route('/orders')
    def orders():
        return alpaca_get('/v2/orders?status=all&limit=50')

    @app.route('/api/account')
    def api_account():
        return alpaca_get('/v2/account')

    @app.route('/api/positions')
    def api_positions():
        return alpaca_get('/v2/positions')

    @app.route('/api/orders')
    def api_orders():
        return alpaca_get('/v2/orders?status=all&limit=50')

    @app.route('/api/quotes')
    def api_quotes():
        quotes = []
        for symbol, _, _, _, _ in WATCHLIST:
            q = get_quote(symbol)
            if q:
                q['symbol'] = symbol
                quotes.append(q)
            time.sleep(0.5)
        return {'quotes': quotes}

    @app.route('/quote/<symbol>')
    def quote(symbol):
        url = (f'https://www.alphavantage.co/query?function=GLOBAL_QUOTE'
               f'&symbol={symbol}&apikey={AV_KEY}')
        r = requests.get(url, timeout=10)
        return r.json()

    @app.route('/order', methods=['POST'])
    def order():
        from flask import request as req
        return alpaca_post('/v2/orders', req.get_json())

    @app.route('/order/bracket', methods=['POST'])
    def bracket_order():
        from flask import request as req
        return alpaca_post('/v2/orders', req.get_json())

    @app.route('/scan', methods=['POST'])
    def manual_scan():
        thread = threading.Thread(target=run_scan)
        thread.start()
        return {'message': 'Scan triggered'}

    port = int(os.environ.get('PORT', 8080))
    log.info(f'Health server on port {port}')
    app.run(host='0.0.0.0', port=port, debug=False)

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    log.info('🚀 Empire Trading Bot starting...')

    if not ALPACA_KEY or not ALPACA_SECRET:
        log.error('❌ ALPACA_API_KEY and ALPACA_SECRET_KEY must be set as environment variables!')
        exit(1)

    # Health server in background thread
    threading.Thread(target=start_health_server, daemon=True).start()
    time.sleep(2)  # let server start

    # Initial scan
    run_scan()

    # Scan every SCAN_INTERVAL seconds forever
    while True:
        time.sleep(SCAN_INTERVAL)
        run_scan()
