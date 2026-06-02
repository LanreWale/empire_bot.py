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
  ALPHAVANTAGE_KEY     — Alpha Vantage API key (REQUIRED for market data)
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

# ── Market data using Alpha Vantage ────────────────────────────────────────────
def get_quote(symbol):
    """Fetch live quote from Alpha Vantage API."""
    if not AV_KEY:
        log.error(f'❌ ALPHAVANTAGE_KEY not set! Cannot fetch quote for {symbol}')
        return None
    
    try:
        url = f'https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={AV_KEY}'
        response = requests.get(url, timeout=10)
        
        if response.status_code != 200:
            log.warning(f'{symbol}: Alpha Vantage HTTP {response.status_code}')
            return None
        
        data = response.json()
        
        # Check for API rate limit
        if 'Note' in data:
            log.warning(f'{symbol}: Alpha Vantage rate limit reached. Waiting 60 seconds...')
            time.sleep(60)
            return None
        
        # Check for error message
        if 'Error Message' in data:
            log.warning(f'{symbol}: Alpha Vantage error - {data["Error Message"]}')
            return None
        
        # Extract quote data
        quote_data = data.get('Global Quote', {})
        if not quote_data:
            log.warning(f'{symbol}: No quote data returned')
            return None
        
        # Parse values
        price = float(quote_data.get('05. price', 0))
        change_pct = float(quote_data.get('10. change percent', '0%').replace('%', ''))
        volume = int(quote_data.get('06. volume', 0))
        
        if price == 0:
            log.warning(f'{symbol}: Price is zero')
            return None
        
        log.info(f'✅ {symbol}: ${price:.2f} ({change_pct:+.2f}%) | Vol: {volume:,}')
        
        return {
            'price': price,
            'change_pct': change_pct,
            'volume': volume,
        }
        
    except requests.exceptions.Timeout:
        log.error(f'{symbol}: Alpha Vantage timeout')
        return None
    except requests.exceptions.ConnectionError:
        log.error(f'{symbol}: Alpha Vantage connection error')
        return None
    except Exception as e:
        log.error(f'{symbol}: Alpha Vantage error - {e}')
        return None

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
                time.sleep(12)  # Rate limit delay
                continue

            action, confidence, reason = generate_signal(strategy, quote)
            price = quote['price']
            log.info(f'{symbol} [{strategy}]: {action.upper()} {confidence}% — {reason}')

            # Only execute high-confidence signals (≥75%) not already held
            if action == 'hold' or confidence < 75:
                time.sleep(12)  # Rate limit delay
                continue
            if symbol in open_symbols and action == 'buy':
                log.info(f'{symbol}: Already holding — skipping buy')
                time.sleep(12)  # Rate limit delay
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
            
            # Alpha Vantage rate limit: 5 calls per minute = 12 seconds between calls
            time.sleep(12)

        except Exception as e:
            log.error(f'{symbol}: Error — {e}')
            time.sleep(12)

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
    from flask import Flask
    app = Flask(__name__)

    # --- NEW HOMEPAGE DASHBOARD ---
    @app.route('/')
    def home():
        return '''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Empire Trading Bot - Command Center</title>
            <style>
                body { font-family: 'Segoe UI', Arial, sans-serif; margin: 0; padding: 0; background: linear-gradient(135deg, #0a0e27 0%, #1a1f3a 100%); color: #e0e0e0; min-height: 100vh; }
                .header { background: rgba(0, 0, 0, 0.3); padding: 20px 40px; border-bottom: 1px solid rgba(255, 215, 0, 0.3); }
                h1 { margin: 0; background: linear-gradient(135deg, #ffd700, #ff8c00); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
                .subtitle { color: #888; margin-top: 5px; }
                .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
                .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 20px; margin-bottom: 20px; }
                .card { background: rgba(255, 255, 255, 0.05); border-radius: 15px; padding: 20px; backdrop-filter: blur(10px); border: 1px solid rgba(255, 255, 255, 0.1); }
                .card h2 { color: #ffd700; margin-top: 0; border-left: 3px solid #ffd700; padding-left: 10px; }
                .status-badge { display: inline-block; padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; margin-right: 10px; }
                .status-live { background: rgba(255, 68, 68, 0.2); color: #ff4444; border: 1px solid #ff4444; }
                .status-good { background: rgba(0, 255, 136, 0.2); color: #00ff88; border: 1px solid #00ff88; }
                .endpoint-list { list-style: none; padding: 0; }
                .endpoint-list li { padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.1); }
                .endpoint-list li a { color: #00ff88; text-decoration: none; font-family: monospace; }
                .endpoint-list li a:hover { text-decoration: underline; }
                .watchlist { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
                .watchlist-item { background: rgba(0,0,0,0.3); padding: 8px 15px; border-radius: 20px; font-size: 14px; }
                .footer { text-align: center; padding: 20px; background: rgba(0, 0, 0, 0.3); margin-top: 20px; font-size: 12px; color: #666; }
            </style>
        </head>
        <body>
            <div class="header">
                <h1>🏛️ Empire Trading Command Center</h1>
                <div class="subtitle">Welcome back, Emperor. Your empire awaits.</div>
            </div>
            <div class="container">
                <div class="grid">
                    <div class="card">
                        <h2>📊 System Status</h2>
                        <p><span class="status-badge status-live">🔴 LIVE TRADING</span> <span class="status-badge status-good">🤖 BOT RUNNING</span></p>
                        <p><strong>Data Source:</strong> Alpha Vantage</p>
                        <p><strong>Scan Interval:</strong> ''' + str(SCAN_INTERVAL) + ''' seconds</p>
                        <p><strong>Watchlist Size:</strong> ''' + str(len(WATCHLIST)) + ''' symbols</p>
                        <p><strong>Last Check:</strong> <span id="timestamp">Loading...</span></p>
                    </div>
                    <div class="card">
                        <h2>📋 Active Watchlist</h2>
                        <div class="watchlist">
                            ''' + ''.join([f'<div class="watchlist-item">{s[0]}</div>' for s in WATCHLIST]) + '''
                        </div>
                        <p style="margin-top: 15px; color: #888;">Strategies: Momentum, Breakout, Mean Reversion, Swing</p>
                    </div>
                </div>
                <div class="grid">
                    <div class="card">
                        <h2>🔗 API Endpoints</h2>
                        <ul class="endpoint-list">
                            <li><a href="/health">🔍 /health</a> - Bot health check</li>
                            <li><a href="/account">💰 /account</a> - Account information</li>
                            <li><a href="/positions">📈 /positions</a> - Open positions</li>
                            <li><a href="/orders">📜 /orders</a> - Order history</li>
                            <li><a href="/quote/AAPL">💹 /quote/&lt;symbol&gt;</a> - Stock quote (AAPL example)</li>
                        </ul>
                    </div>
                    <div class="card">
                        <h2>⚙️ Configuration</h2>
                        <p><strong>Alpha Vantage:</strong> ✅ Configured</p>
                        <p><strong>Alpaca API:</strong> ✅ Configured</p>
                        <p><strong>Telegram:</strong> ''' + ('✅ Configured' if TG_TOKEN and TG_CHAT else '⚠️ Not configured') + '''</p>
                        <p><strong>Rate Limit:</strong> 5 calls/minute (12s delay between symbols)</p>
                        <p><strong>Max Position:</strong> $500 per trade</p>
                        <p><strong>Min Confidence:</strong> 75%</p>
                    </div>
                </div>
            </div>
            <div class="footer">
                <p>⚡ Empire Trading Bot | Live 24/7 | Deployed on Render.com</p>
                <p style="font-size: 11px;">Alpha Vantage Free Tier | ⚠️ REAL MONEY IS BEING TRADED</p>
            </div>
            <script>
                document.getElementById('timestamp').innerHTML = new Date().toLocaleString();
                setInterval(() => {
                    document.getElementById('timestamp').innerHTML = new Date().toLocaleString();
                }, 1000);
            </script>
        </body>
        </html>
        '''

    @app.route('/health')
    def health():
        return {
            'status': 'running', 
            'bot': 'Empire Trading Bot', 
            'time': datetime.utcnow().isoformat(),
            'data_source': 'Alpha Vantage',
            'alpha_vantage_configured': bool(AV_KEY),
            'watchlist_size': len(WATCHLIST),
            'scan_interval': SCAN_INTERVAL
        }

    @app.route('/account')
    def account():
        return alpaca_get('/v2/account')

    @app.route('/positions')
    def positions():
        return alpaca_get('/v2/positions')

    @app.route('/orders')
    def orders():
        return alpaca_get('/v2/orders?status=all&limit=50')

    @app.route('/quote/<symbol>')
    def quote(symbol):
        """Get quote using Alpha Vantage."""
        quote_data = get_quote(symbol.upper())
        if quote_data:
            return quote_data
        return {'error': 'Could not fetch quote'}, 404

    @app.route('/order', methods=['POST'])
    def order():
        from flask import request as req
        return alpaca_post('/v2/orders', req.get_json())

    @app.route('/order/bracket', methods=['POST'])
    def bracket_order():
        from flask import request as req
        return alpaca_post('/v2/orders', req.get_json())

    port = int(os.environ.get('PORT', 8080))
    log.info(f'Health server on port {port}')
    app.run(host='0.0.0.0', port=port, debug=False)

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    log.info('🚀 Empire Trading Bot starting...')
    log.info('=' * 50)
    
    # Validate Alpha Vantage API key
    if not AV_KEY:
        log.error('❌ ALPHAVANTAGE_KEY environment variable is REQUIRED!')
        log.error('   Get a free API key at: https://www.alphavantage.co/support/#api-key')
        log.error('   Then add it to your Render environment variables.')
        exit(1)
    else:
        log.info('✅ Alpha Vantage API key configured')
    
    # Validate Alpaca credentials
    if not ALPACA_KEY or not ALPACA_SECRET:
        log.error('❌ ALPACA_API_KEY and ALPACA_SECRET_KEY must be set!')
        exit(1)
    else:
        log.info('✅ Alpaca API credentials configured')
    
    # Display configuration
    log.info(f'📊 Market Data Source: Alpha Vantage')
    log.info(f'⏱️ Scan interval: {SCAN_INTERVAL} seconds')
    log.info(f'📋 Watchlist: {len(WATCHLIST)} symbols - {", ".join([s[0] for s in WATCHLIST])}')
    log.info(f'⚠️  Alpha Vantage free tier: 5 API calls per minute')
    log.info(f'⏲️  Estimated scan time: ~{len(WATCHLIST) * 12} seconds with rate limiting')
    log.info('=' * 50)
    
    # Send startup notification via Telegram
    if TG_TOKEN and TG_CHAT:
        send_telegram(
            f'🤖 <b>Empire Trading Bot Started</b>\n'
            f'📊 Data Source: Alpha Vantage\n'
            f'⏱️ Scan every {SCAN_INTERVAL} seconds\n'
            f'📋 Watching {len(WATCHLIST)} symbols\n'
            f'💰 Trading Mode: LIVE'
        )
    
    # Start health server in background thread
    log.info('Starting health server...')
    threading.Thread(target=start_health_server, daemon=True).start()
    time.sleep(2)
    
    # Run initial scan
    log.info('Running initial scan...')
    run_scan()
    
    # Main loop
    log.info(f'Entering main loop. Will scan every {SCAN_INTERVAL} seconds.')
    while True:
        time.sleep(SCAN_INTERVAL)
        run_scan()
