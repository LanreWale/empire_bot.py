"""
╔══════════════════════════════════════════════════════════════════╗
║          EMPIRE STOCK TRADING SYSTEM — LIVE TRADING             ║
║          Deployed on Render.com - 24/7 Automated Bot            ║
║                                                                  ║
║  ⚠️  LIVE TRADING MODE - Real money will be used!              ║
║  ⚠️  Ensure sufficient funds in your Alpaca account            ║
╚══════════════════════════════════════════════════════════════════╝

REQUIRED ENV VARS on Render:
  ALPACA_API_KEY       — Your Alpaca LIVE API key
  ALPACA_SECRET_KEY    — Your Alpaca LIVE secret key
  ALPACA_BASE_URL      — https://api.alpaca.markets (LIVE)
  TELEGRAM_BOT_TOKEN   — Telegram bot token (strongly recommended)
  TELEGRAM_CHAT_ID     — Your Telegram chat ID
  SCAN_INTERVAL_SECONDS — Seconds between scans (default: 300)

OPTIONAL ENV VARS:
  MAX_POSITION_USD     — Max USD per trade (default: 500)
  MIN_CONFIDENCE       — Minimum confidence to trade (default: 75)
"""

import os
import time
import logging
import threading
import json
from datetime import datetime, timezone
from typing import Dict, Optional, Set, Tuple

import requests
import yfinance as yf
from flask import Flask, request as flask_request, jsonify

# ── Logging Configuration ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('EmpireBot')

# ── Environment Variables ──────────────────────────────────────────────────────
ALPACA_KEY = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_URL = os.environ.get('ALPACA_BASE_URL', 'https://api.alpaca.markets')  # LIVE
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '')

# Parse scan interval with error handling
try:
    SCAN_INTERVAL = int(os.environ.get('SCAN_INTERVAL_SECONDS', '300'))
except ValueError:
    log.warning("Invalid SCAN_INTERVAL_SECONDS, using default 300")
    SCAN_INTERVAL = 300

# Trading parameters
MAX_POSITION_USD = float(os.environ.get('MAX_POSITION_USD', '500'))
MIN_CONFIDENCE = int(os.environ.get('MIN_CONFIDENCE', '75'))

# ── Watchlist Configuration ────────────────────────────────────────────────────
# Format: (symbol, strategy, max_position_usd, stop_loss_percent, take_profit_percent)
WATCHLIST = [
    ('AAPL', 'momentum', MAX_POSITION_USD, 2.0, 4.0),
    ('TSLA', 'momentum', MAX_POSITION_USD, 2.5, 5.0),
    ('NVDA', 'breakout', MAX_POSITION_USD, 2.0, 5.0),
    ('MSFT', 'mean_reversion', MAX_POSITION_USD, 2.0, 4.0),
    ('AMZN', 'swing', MAX_POSITION_USD, 2.0, 4.0),
    ('GOOGL', 'momentum', MAX_POSITION_USD, 2.0, 4.0),
    ('META', 'breakout', MAX_POSITION_USD, 2.5, 5.0),
    ('SPY', 'mean_reversion', MAX_POSITION_USD, 1.5, 3.0),
]

# ── Alpaca API Headers ─────────────────────────────────────────────────────────
HEADERS = {
    'APCA-API-KEY-ID': ALPACA_KEY,
    'APCA-API-SECRET-KEY': ALPACA_SECRET,
    'Content-Type': 'application/json',
}

# ── Alpaca API Helpers ─────────────────────────────────────────────────────────
def alpaca_get(path: str) -> Dict:
    """Make GET request to Alpaca API."""
    try:
        response = requests.get(f'{ALPACA_URL}{path}', headers=HEADERS, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        log.error(f'Alpaca GET failed: {e}')
        raise

def alpaca_post(path: str, body: Dict) -> Dict:
    """Make POST request to Alpaca API."""
    try:
        response = requests.post(f'{ALPACA_URL}{path}', headers=HEADERS,
                                data=json.dumps(body), timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        log.error(f'Alpaca POST failed: {e}')
        raise

def get_account() -> Dict:
    """Get account information."""
    return alpaca_get('/v2/account')

def get_positions() -> list:
    """Get all open positions."""
    try:
        return alpaca_get('/v2/positions')
    except:
        return []

def get_open_symbols() -> Set[str]:
    """Get set of symbols currently held."""
    try:
        positions = get_positions()
        return {p['symbol'] for p in positions}
    except:
        return set()

def place_bracket_order(symbol: str, qty: int, side: str, price: float, 
                       sl_pct: float, tp_pct: float) -> Dict:
    """
    Place a bracket order with stop-loss and take-profit.
    
    Args:
        symbol: Stock symbol
        qty: Number of shares
        side: 'buy' or 'sell'
        price: Current price
        sl_pct: Stop-loss percentage
        tp_pct: Take-profit percentage
    """
    if side == 'buy':
        stop_price = round(price * (1 - sl_pct / 100), 2)
        limit_price = round(price * (1 + tp_pct / 100), 2)
    else:  # sell
        stop_price = round(price * (1 + sl_pct / 100), 2)
        limit_price = round(price * (1 - tp_pct / 100), 2)
    
    body = {
        'symbol': symbol,
        'qty': str(qty),
        'side': side,
        'type': 'market',
        'time_in_force': 'day',
        'order_class': 'bracket',
        'stop_loss': {'stop_price': str(stop_price)},
        'take_profit': {'limit_price': str(limit_price)},
    }
    return alpaca_post('/v2/orders', body)

# ── Market Data (Yahoo Finance) ────────────────────────────────────────────────
def get_quote(symbol: str) -> Optional[Dict]:
    """
    Fetch live quote from Yahoo Finance.
    No API key required, no rate limits.
    """
    try:
        ticker = yf.Ticker(symbol)
        
        # Get current price from ticker info
        info = ticker.info
        
        # Try multiple possible price fields
        price = (info.get('regularMarketPrice') or 
                info.get('currentPrice') or 
                info.get('ask') or 
                info.get('bid') or 0)
        
        # Get previous close for change percentage
        prev_close = (info.get('regularMarketPreviousClose') or 
                     info.get('previousClose') or price)
        
        # Fallback to history method if info doesn't have price
        if price == 0:
            hist = ticker.history(period='2d', interval='1d')
            if not hist.empty:
                price = float(hist['Close'].iloc[-1])
                if len(hist) > 1:
                    prev_close = float(hist['Close'].iloc[-2])
                else:
                    prev_close = price
        
        if price == 0:
            log.warning(f'{symbol}: Could not fetch price')
            return None
        
        # Calculate change percentage
        if prev_close and prev_close > 0:
            change_pct = ((price - prev_close) / prev_close) * 100
        else:
            change_pct = 0
        
        # Get volume
        volume = info.get('volume', info.get('regularMarketVolume', 0))
        
        log.info(f'✅ {symbol}: ${price:.2f} ({change_pct:+.2f}%) | Vol: {volume:,}')
        
        return {
            'price': price,
            'change_pct': change_pct,
            'volume': volume,
        }
        
    except Exception as e:
        log.error(f'{symbol}: Yahoo Finance error - {str(e)[:100]}')
        return None

# ── Market Hours Check ─────────────────────────────────────────────────────────
def market_is_open() -> bool:
    """Check if the stock market is currently open."""
    try:
        clock = alpaca_get('/v2/clock')
        is_open = clock.get('is_open', False)
        next_open = clock.get('next_open', 'unknown')
        next_close = clock.get('next_close', 'unknown')
        
        if is_open:
            log.info('📈 Market is OPEN')
        else:
            log.info(f'🔒 Market is CLOSED. Next open: {next_open}')
        return is_open
    except Exception as e:
        log.warning(f'Could not check market hours: {e}')
        return True  # Assume open if can't check

# ── Signal Generation ─────────────────────────────────────────────────────────
def generate_signal(strategy: str, quote: Dict) -> Tuple[str, int, str]:
    """
    Generate trading signal based on strategy.
    
    Returns:
        Tuple of (action, confidence, reason)
        action: 'buy', 'sell', or 'hold'
        confidence: 0-100
        reason: Description of the signal
    """
    change_pct = quote['change_pct']
    volume = quote['volume']
    
    if strategy == 'momentum':
        if change_pct > 1.5:
            confidence = min(95, int(60 + change_pct * 8))
            return ('buy', confidence, f'Strong momentum +{change_pct:.2f}%')
        elif change_pct < -1.5:
            confidence = min(95, int(60 + abs(change_pct) * 8))
            return ('sell', confidence, f'Weak momentum {change_pct:.2f}%')
            
    elif strategy == 'mean_reversion':
        if change_pct < -2.5:
            return ('buy', 78, f'Oversold bounce {change_pct:.2f}%')
        elif change_pct > 2.5:
            return ('sell', 78, f'Overbought pullback +{change_pct:.2f}%')
            
    elif strategy == 'breakout':
        if change_pct > 2.0 and volume > 5_000_000:
            return ('buy', 85, f'Breakout with volume {volume/1e6:.1f}M')
        elif change_pct < -2.0 and volume > 5_000_000:
            return ('sell', 80, f'Breakdown with volume {volume/1e6:.1f}M')
            
    elif strategy == 'swing':
        if change_pct > 1.0:
            return ('buy', 72, f'Swing entry +{change_pct:.2f}%')
        elif change_pct < -1.0:
            return ('sell', 72, f'Swing exit {change_pct:.2f}%')
    
    return ('hold', 40, 'No clear signal')

# ── Telegram Alerts ────────────────────────────────────────────────────────────
def send_telegram(message: str) -> None:
    """Send alert message to Telegram."""
    if not TG_TOKEN or not TG_CHAT:
        return
    
    try:
        url = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
        payload = {
            'chat_id': TG_CHAT,
            'text': message,
            'parse_mode': 'HTML'
        }
        requests.post(url, json=payload, timeout=10)
        log.info('📱 Telegram alert sent')
    except Exception as e:
        log.warning(f'Telegram error: {e}')

# ── Account Summary ─────────────────────────────────────────────────────────
def send_account_summary() -> None:
    """Send current account status."""
    try:
        account = get_account()
        equity = float(account.get('equity', 0))
        cash = float(account.get('cash', 0))
        buying_power = float(account.get('buying_power', 0))
        daily_pnl = float(account.get('daily_pnl', 0))
        
        message = (
            f'💰 <b>Empire Bot Account Status</b>\n'
            f'┌─────────────────────┐\n'
            f'│ Equity:      ${equity:,.2f}\n'
            f'│ Cash:        ${cash:,.2f}\n'
            f'│ Buying Power: ${buying_power:,.2f}\n'
            f'│ Daily P&L:   {"+" if daily_pnl >= 0 else ""}${daily_pnl:,.2f}\n'
            f'└─────────────────────┘'
        )
        send_telegram(message)
    except Exception as e:
        log.error(f'Failed to send account summary: {e}')

# ── Main Scan Loop ────────────────────────────────────────────────────────────
def run_scan() -> None:
    """Execute one scan cycle: check signals and place orders."""
    log.info('─── Starting scan cycle ───')
    
    # Check if market is open
    if not market_is_open():
        log.info('Market is closed — skipping scan')
        return
    
    # Get currently held symbols
    open_symbols = get_open_symbols()
    log.info(f'Currently holding: {open_symbols if open_symbols else "None"}')
    
    executed_orders = 0
    
    for symbol, strategy, max_pos, sl_pct, tp_pct in WATCHLIST:
        try:
            # Fetch current quote
            quote = get_quote(symbol)
            if not quote:
                log.warning(f'{symbol}: Could not fetch quote — skipping')
                continue
            
            # Generate trading signal
            action, confidence, reason = generate_signal(strategy, quote)
            current_price = quote['price']
            
            log.info(f'{symbol} [{strategy}]: {action.upper()} ({confidence}%) — {reason}')
            
            # Skip if no action or low confidence
            if action == 'hold':
                continue
            if confidence < MIN_CONFIDENCE:
                log.info(f'{symbol}: Confidence {confidence}% < {MIN_CONFIDENCE}% — skipping')
                continue
            
            # Skip buy if already holding
            if action == 'buy' and symbol in open_symbols:
                log.info(f'{symbol}: Already holding — skipping buy')
                continue
            
            # Calculate quantity to trade
            qty = max(1, int(max_pos / current_price))
            
            # Get account buying power to ensure we can trade
            account = get_account()
            buying_power = float(account.get('buying_power', 0))
            order_value = qty * current_price
            
            if order_value > buying_power:
                log.warning(f'{symbol}: Order value ${order_value:,.2f} exceeds buying power ${buying_power:,.2f}')
                continue
            
            # Place the order
            order = place_bracket_order(symbol, qty, action, current_price, sl_pct, tp_pct)
            
            # Calculate stop loss and take profit prices
            if action == 'buy':
                stop_price = round(current_price * (1 - sl_pct / 100), 2)
                target_price = round(current_price * (1 + tp_pct / 100), 2)
            else:
                stop_price = round(current_price * (1 + sl_pct / 100), 2)
                target_price = round(current_price * (1 - tp_pct / 100), 2)
            
            log.info(f'✅ ORDER PLACED: {action.upper()} {qty}x {symbol} '
                    f'@ ${current_price:.2f} | SL: ${stop_price} | TP: ${target_price}')
            
            # Send Telegram alert
            alert = (
                f'🔥 <b>LIVE TRADE EXECUTED</b>\n'
                f'{"🟢 BUY" if action == "buy" else "🔴 SELL"} <b>{qty}x {symbol}</b>\n'
                f'💰 Price: ${current_price:.2f}\n'
                f'💵 Value: ${order_value:,.2f}\n'
                f'📉 Stop Loss: ${stop_price} ({sl_pct:.1f}%)\n'
                f'📈 Take Profit: ${target_price} ({tp_pct:.1f}%)\n'
                f'🎯 Strategy: {strategy} | Confidence: {confidence}%\n'
                f'📝 Reason: {reason}'
            )
            send_telegram(alert)
            
            executed_orders += 1
            open_symbols.add(symbol)  # Update held symbols
            
            # Small delay between orders to avoid rate limits
            time.sleep(1)
            
        except Exception as e:
            log.error(f'{symbol}: Error processing - {e}')
            send_telegram(f'⚠️ <b>Error processing {symbol}</b>\n{str(e)[:200]}')
    
    log.info(f'─── Scan complete. {executed_orders} order(s) placed ───')

# ── Health Check Server (Keeps Render awake) ──────────────────────────────────
def start_health_server() -> None:
    """Start Flask server for health checks and manual API access."""
    app = Flask(__name__)
    
    @app.route('/health')
    def health():
        return jsonify({
            'status': 'running',
            'bot': 'Empire Trading Bot - LIVE',
            'time': datetime.now(timezone.utc).isoformat(),
            'version': '2.0-live'
        })
    
    @app.route('/')
    def home():
        return jsonify({
            'message': 'Empire Trading Bot is running in LIVE mode',
            'endpoints': ['/health', '/account', '/positions', '/orders', '/quote/<symbol>', '/scan'],
            'warning': '⚠️ REAL MONEY IS BEING TRADED ⚠️'
        })
    
    @app.route('/account')
    def account():
        try:
            return jsonify(get_account())
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    @app.route('/positions')
    def positions():
        try:
            return jsonify(get_positions())
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    @app.route('/orders')
    def orders():
        try:
            return jsonify(alpaca_get('/v2/orders?status=all&limit=50'))
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    @app.route('/quote/<symbol>')
    def quote_endpoint(symbol):
        try:
            quote = get_quote(symbol.upper())
            if quote:
                return jsonify(quote)
            return jsonify({'error': 'Could not fetch quote'}), 404
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    @app.route('/scan', methods=['POST'])
    def manual_scan():
        """Manually trigger a scan cycle."""
        try:
            thread = threading.Thread(target=run_scan)
            thread.start()
            return jsonify({'message': 'Scan triggered'}), 202
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    port = int(os.environ.get('PORT', 8080))
    log.info(f'🌐 Health server running on port {port}')
    app.run(host='0.0.0.0', port=port, debug=False)

# ── Main Entry Point ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    log.info('🚀 Empire Trading Bot starting in LIVE mode...')
    log.warning('⚠️  ⚠️  ⚠️  LIVE TRADING ACTIVE - REAL MONEY WILL BE USED ⚠️  ⚠️  ⚠️')
    log.info(f'📊 Watchlist: {len(WATCHLIST)} symbols')
    log.info(f'⏱️  Scan interval: {SCAN_INTERVAL} seconds')
    log.info(f'💰 Max position: ${MAX_POSITION_USD:,.2f}')
    log.info(f'🎯 Min confidence: {MIN_CONFIDENCE}%')
    
    # Validate required environment variables
    if not ALPACA_KEY or not ALPACA_SECRET:
        log.error('❌ ALPACA_API_KEY and ALPACA_SECRET_KEY must be set!')
        log.error('Please add these environment variables in Render dashboard')
        exit(1)
    
    # Verify we're using live endpoint
    if 'paper' in ALPACA_URL.lower():
        log.error('❌ ALPACA_BASE_URL is set to paper trading but you requested live trading!')
        log.error('Please set ALPACA_BASE_URL=https://api.alpaca.markets')
        exit(1)
    else:
        log.warning('🔴 RUNNING IN LIVE TRADING MODE - REAL MONEY')
    
    # Send startup notification with account summary
    send_telegram(
        f'🔥 <b>Empire Trading Bot - LIVE MODE ACTIVATED</b>\n'
        f'⚠️ <b>REAL MONEY IS BEING TRADED</b>\n'
        f'📊 Watchlist: {len(WATCHLIST)} symbols\n'
        f'⏱️  Scan every {SCAN_INTERVAL} seconds\n'
        f'💰 Max position: ${MAX_POSITION_USD:,.2f}\n'
        f'🎯 Min confidence: {MIN_CONFIDENCE}%'
    )
    
    # Send initial account status
    time.sleep(2)
    send_account_summary()
    
    # Start health server in background
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    time.sleep(2)  # Allow server to start
    
    # Run initial scan
    log.info('Running initial scan...')
    run_scan()
    
    # Main loop - scan forever
    log.info(f'Entering main loop. Will scan every {SCAN_INTERVAL} seconds.')
    while True:
        time.sleep(SCAN_INTERVAL)
        run_scan()
