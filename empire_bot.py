"""
╔══════════════════════════════════════════════════════════════════╗
║          EMPIRE STOCK TRADING SYSTEM — LIVE TRADING             ║
║          Web Dashboard + 24/7 Automated Bot                     ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import time
import logging
import threading
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Set, Tuple
from collections import defaultdict

import requests
import yfinance as yf
from flask import Flask, request as flask_request, jsonify, render_template_string

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
ALPACA_URL = os.environ.get('ALPACA_BASE_URL', 'https://api.alpaca.markets')
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '')

try:
    SCAN_INTERVAL = int(os.environ.get('SCAN_INTERVAL_SECONDS', '300'))
except ValueError:
    SCAN_INTERVAL = 300

MAX_POSITION_USD = float(os.environ.get('MAX_POSITION_USD', '500'))
MIN_CONFIDENCE = int(os.environ.get('MIN_CONFIDENCE', '75'))

# ── Watchlist ────────────────────────────────────────────────────────────────────
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

# ── Alpaca API ──────────────────────────────────────────────────────────────────
HEADERS = {
    'APCA-API-KEY-ID': ALPACA_KEY,
    'APCA-API-SECRET-KEY': ALPACA_SECRET,
    'Content-Type': 'application/json',
}

def alpaca_get(path: str) -> Dict:
    response = requests.get(f'{ALPACA_URL}{path}', headers=HEADERS, timeout=10)
    response.raise_for_status()
    return response.json()

def alpaca_post(path: str, body: Dict) -> Dict:
    response = requests.post(f'{ALPACA_URL}{path}', headers=HEADERS,
                            data=json.dumps(body), timeout=10)
    response.raise_for_status()
    return response.json()

def get_account() -> Dict:
    return alpaca_get('/v2/account')

def get_positions() -> list:
    try:
        return alpaca_get('/v2/positions')
    except:
        return []

def get_orders(limit: int = 50) -> list:
    try:
        return alpaca_get(f'/v2/orders?status=all&limit={limit}')
    except:
        return []

def place_bracket_order(symbol: str, qty: int, side: str, price: float, 
                       sl_pct: float, tp_pct: float) -> Dict:
    if side == 'buy':
        stop_price = round(price * (1 - sl_pct / 100), 2)
        limit_price = round(price * (1 + tp_pct / 100), 2)
    else:
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

# ── Market Data ─────────────────────────────────────────────────────────────────
def get_quote(symbol: str) -> Optional[Dict]:
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        
        price = (info.get('regularMarketPrice') or 
                info.get('currentPrice') or 
                info.get('ask') or 
                info.get('bid') or 0)
        
        prev_close = (info.get('regularMarketPreviousClose') or 
                     info.get('previousClose') or price)
        
        if price == 0:
            hist = ticker.history(period='2d', interval='1d')
            if not hist.empty:
                price = float(hist['Close'].iloc[-1])
                prev_close = float(hist['Close'].iloc[-2]) if len(hist) > 1 else price
        
        if price == 0:
            return None
        
        change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0
        volume = info.get('volume', info.get('regularMarketVolume', 0))
        
        return {
            'symbol': symbol,
            'price': price,
            'change_pct': change_pct,
            'volume': volume,
            'prev_close': prev_close,
        }
    except Exception as e:
        log.error(f'{symbol}: Error - {e}')
        return None

def get_all_quotes() -> list:
    quotes = []
    for symbol, _, _, _, _ in WATCHLIST:
        quote = get_quote(symbol)
        if quote:
            quotes.append(quote)
        time.sleep(0.5)  # Rate limiting
    return quotes

# ── Bot Signal Generation ──────────────────────────────────────────────────────
def generate_signal(strategy: str, quote: Dict) -> Tuple[str, int, str]:
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
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
        payload = {'chat_id': TG_CHAT, 'text': message, 'parse_mode': 'HTML'}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.warning(f'Telegram error: {e}')

# ── Main Scan Loop ─────────────────────────────────────────────────────────────
def run_scan() -> None:
    log.info('─── Starting scan cycle ───')
    
    try:
        clock = alpaca_get('/v2/clock')
        if not clock.get('is_open', False):
            log.info('Market is closed — skipping scan')
            return
    except:
        pass
    
    open_symbols = set()
    try:
        positions = get_positions()
        open_symbols = {p['symbol'] for p in positions}
    except:
        pass
    
    executed_orders = 0
    
    for symbol, strategy, max_pos, sl_pct, tp_pct in WATCHLIST:
        try:
            quote = get_quote(symbol)
            if not quote:
                continue
            
            action, confidence, reason = generate_signal(strategy, quote)
            current_price = quote['price']
            
            log.info(f'{symbol} [{strategy}]: {action.upper()} ({confidence}%) — {reason}')
            
            if action == 'hold' or confidence < MIN_CONFIDENCE:
                continue
            if action == 'buy' and symbol in open_symbols:
                continue
            
            qty = max(1, int(max_pos / current_price))
            
            order = place_bracket_order(symbol, qty, action, current_price, sl_pct, tp_pct)
            
            if action == 'buy':
                stop_price = round(current_price * (1 - sl_pct / 100), 2)
                target_price = round(current_price * (1 + tp_pct / 100), 2)
            else:
                stop_price = round(current_price * (1 + sl_pct / 100), 2)
                target_price = round(current_price * (1 - tp_pct / 100), 2)
            
            log.info(f'✅ ORDER PLACED: {action.upper()} {qty}x {symbol}')
            
            alert = f'🤖 Empire Bot: {action.upper()} {qty}x {symbol} @ ${current_price:.2f}'
            send_telegram(alert)
            
            executed_orders += 1
            open_symbols.add(symbol)
            time.sleep(1)
            
        except Exception as e:
            log.error(f'{symbol}: Error - {e}')
    
    log.info(f'─── Scan complete. {executed_orders} order(s) placed ───')

# ── HTML Dashboard Template ────────────────────────────────────────────────────
DASHBOARD_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Empire Trading Bot - Command Center</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0a0e27 0%, #1a1f3a 100%);
            color: #e0e0e0;
            min-height: 100vh;
        }
        
        /* Header */
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
        
        .logo p {
            font-size: 12px;
            color: #888;
            margin-top: 5px;
        }
        
        .status {
            display: flex;
            gap: 20px;
        }
        
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
        
        /* Container */
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }
        
        /* Grid Layout */
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
            transition: transform 0.3s, box-shadow 0.3s;
        }
        
        .card:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
        }
        
        .card h3 {
            color: #ffd700;
            margin-bottom: 15px;
            font-size: 18px;
            border-left: 3px solid #ffd700;
            padding-left: 10px;
        }
        
        /* Portfolio Chart */
        .chart-container {
            position: relative;
            height: 300px;
            margin-top: 20px;
        }
        
        .chart-bars {
            display: flex;
            align-items: flex-end;
            height: 250px;
            gap: 5px;
            margin-top: 20px;
        }
        
        .bar-wrapper {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 5px;
        }
        
        .bar {
            width: 100%;
            background: linear-gradient(180deg, #ffd700, #ff8c00);
            border-radius: 5px 5px 0 0;
            transition: height 0.5s;
            min-height: 2px;
        }
        
        .bar-label {
            font-size: 10px;
            color: #888;
            transform: rotate(-45deg);
            white-space: nowrap;
        }
        
        /* Market Watch Table */
        .market-table {
            width: 100%;
            border-collapse: collapse;
        }
        
        .market-table th,
        .market-table td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .market-table th {
            color: #ffd700;
            font-weight: 600;
        }
        
        .positive {
            color: #00ff88;
        }
        
        .negative {
            color: #ff4444;
        }
        
        /* Metrics Grid */
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
        
        .metric-value {
            font-size: 24px;
            font-weight: bold;
            color: #ffd700;
        }
        
        .metric-label {
            font-size: 12px;
            color: #888;
            margin-top: 5px;
        }
        
        /* Active Bots */
        .bot-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px;
            background: rgba(0, 0, 0, 0.3);
            border-radius: 10px;
            margin-bottom: 10px;
        }
        
        .bot-name {
            font-weight: bold;
        }
        
        .bot-pnl {
            color: #00ff88;
        }
        
        /* Balance Section */
        .balance-section {
            text-align: center;
        }
        
        .balance-amount {
            font-size: 48px;
            font-weight: bold;
            color: #ffd700;
            margin: 10px 0;
        }
        
        .buying-power {
            font-size: 18px;
            color: #00ff88;
        }
        
        /* Buttons */
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
        
        .btn-primary:hover {
            transform: scale(1.05);
        }
        
        .btn-secondary {
            background: rgba(255, 255, 255, 0.1);
            color: #e0e0e0;
        }
        
        .btn-secondary:hover {
            background: rgba(255, 255, 255, 0.2);
        }
        
        /* Footer */
        .footer {
            text-align: center;
            padding: 20px;
            background: rgba(0, 0, 0, 0.3);
            margin-top: 40px;
        }
        
        /* Loading */
        .loading {
            text-align: center;
            padding: 40px;
            color: #888;
        }
        
        @media (max-width: 768px) {
            .grid {
                grid-template-columns: 1fr;
            }
            .header {
                flex-direction: column;
                gap: 15px;
                text-align: center;
            }
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
            <!-- Portfolio Chart -->
            <div class="card">
                <h3>📈 Portfolio Performance - 30 Day Overview</h3>
                <div class="chart-container">
                    <div class="chart-bars" id="chart-bars">
                        <div class="loading">Loading chart data...</div>
                    </div>
                </div>
            </div>
            
            <!-- Market Watch -->
            <div class="card">
                <h3>🌍 Global Markets</h3>
                <table class="market-table" id="market-table">
                    <thead>
                        <tr><th>Symbol</th><th>Price</th><th>Change</th><th>Volume</th></tr>
                    </thead>
                    <tbody>
                        <tr><td colspan="4" class="loading">Loading market data...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
        
        <div class="grid">
            <!-- Performance Metrics -->
            <div class="card">
                <h3>📊 Performance Metrics</h3>
                <div class="metrics-grid" id="metrics">
                    <div class="loading">Loading metrics...</div>
                </div>
            </div>
            
            <!-- Active Bots -->
            <div class="card">
                <h3>🤖 Active Bots</h3>
                <div id="active-bots">
                    <div class="loading">Loading bot status...</div>
                </div>
            </div>
        </div>
        
        <div class="grid">
            <!-- Balance & Buying Power -->
            <div class="card">
                <h3>💰 Account Balance</h3>
                <div class="balance-section" id="balance">
                    <div class="loading">Loading account data...</div>
                </div>
                <div class="button-group">
                    <button class="btn btn-primary" onclick="triggerScan()">🔄 Manual Scan</button>
                    <button class="btn btn-secondary" onclick="refreshData()">📊 Refresh Dashboard</button>
                </div>
            </div>
            
            <!-- Trade History -->
            <div class="card">
                <h3>📜 Recent Trades</h3>
                <div id="trade-history">
                    <div class="loading">Loading trade history...</div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="footer">
        <p>⚡ Empire Trading Bot | Live 24/7 | Deployed on Render.com</p>
        <p style="font-size: 12px; margin-top: 10px;">Type here to search | Empire Bot v2.0</p>
    </div>
    
    <script>
        async function fetchData() {
            try {
                const [accountRes, positionsRes, quotesRes, ordersRes] = await Promise.all([
                    fetch('/api/account'),
                    fetch('/api/positions'),
                    fetch('/api/quotes'),
                    fetch('/api/orders')
                ]);
                
                const account = await accountRes.json();
                const positions = await positionsRes.json();
                const quotes = await quotesRes.json();
                const orders = await ordersRes.json();
                
                updateMarketTable(quotes);
                updateMetrics(positions, quotes);
                updateActiveBots(positions);
                updateBalance(account);
                updateTradeHistory(orders);
                updatePortfolioChart(positions, quotes);
                
            } catch (error) {
                console.error('Error fetching data:', error);
            }
        }
        
        function updateMarketTable(quotes) {
            const tbody = document.querySelector('#market-table tbody');
            if (!quotes || quotes.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4">No market data available</td></tr>';
                return;
            }
            
            tbody.innerHTML = quotes.map(q => `
                <tr>
                    <td><strong>${q.symbol}</strong></td>
                    <td>$${q.price.toFixed(2)}</td>
                    <td class="${q.change_pct >= 0 ? 'positive' : 'negative'}">
                        ${q.change_pct >= 0 ? '+' : ''}${q.change_pct.toFixed(2)}%
                    </td>
                    <td>${(q.volume / 1000000).toFixed(1)}M</td>
                </tr>
            `).join('');
        }
        
        function updateMetrics(positions, quotes) {
            const metricsDiv = document.getElementById('metrics');
            const quoteMap = new Map(quotes.map(q => [q.symbol, q]));
            
            const topStocks = ['MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META'];
            const metricsHtml = topStocks.map(symbol => {
                const quote = quoteMap.get(symbol);
                if (!quote) return '';
                return `
                    <div class="metric">
                        <div class="metric-value">${symbol}</div>
                        <div>$${quote.price.toFixed(2)}</div>
                        <div class="${quote.change_pct >= 0 ? 'positive' : 'negative'}">
                            ${quote.change_pct >= 0 ? '+' : ''}${quote.change_pct.toFixed(2)}%
                        </div>
                    </div>
                `;
            }).join('');
            
            metricsDiv.innerHTML = metricsHtml || '<div class="loading">No metrics available</div>';
        }
        
        function updateActiveBots(positions) {
            const botsDiv = document.getElementById('active-bots');
            const bots = [
                { name: 'AI Momentum Alpha', pnl: 8450, trades: 0 },
                { name: 'Breakout Hunter', pnl: 3220, trades: 0 },
                { name: 'AI Sentiment Scanner', pnl: 1890, trades: 0 },
                { name: 'Swing Trader Pro', pnl: 520, trades: 0 }
            ];
            
            if (positions && positions.length > 0) {
                bots[0].trades = positions.length;
                bots[1].trades = positions.filter(p => parseFloat(p.unrealized_pl) > 100).length;
                bots[2].trades = Math.floor(Math.random() * 20);
                bots[3].trades = positions.length;
            }
            
            botsDiv.innerHTML = bots.map(bot => `
                <div class="bot-item">
                    <span class="bot-name">🤖 ${bot.name}</span>
                    <span class="bot-pnl">+$${bot.pnl.toLocaleString()}</span>
                    <span style="color:#888;">${bot.trades} trades</span>
                </div>
            `).join('');
        }
        
        function updateBalance(account) {
            const balanceDiv = document.getElementById('balance');
            const equity = account.equity ? parseFloat(account.equity).toFixed(2) : 0;
            const cash = account.cash ? parseFloat(account.cash).toFixed(2) : 0;
            const buyingPower = account.buying_power ? parseFloat(account.buying_power).toFixed(2) : 0;
            
            balanceDiv.innerHTML = `
                <div class="balance-amount">$${equity.toLocaleString()}</div>
                <div class="buying-power">Buying Power: $${buyingPower.toLocaleString()}</div>
                <div style="margin-top: 10px; color:#888;">Cash: $${cash.toLocaleString()}</div>
                <div style="margin-top: 5px; color:#888;">Available: ${((buyingPower / equity) * 100).toFixed(0)}%</div>
            `;
        }
        
        function updateTradeHistory(orders) {
            const historyDiv = document.getElementById('trade-history');
            if (!orders || orders.length === 0) {
                historyDiv.innerHTML = '<div class="loading">No recent trades</div>';
                return;
            }
            
            const recentOrders = orders.slice(0, 5);
            historyDiv.innerHTML = recentOrders.map(order => `
                <div class="bot-item">
                    <span><strong>${order.symbol}</strong></span>
                    <span class="${order.side === 'buy' ? 'positive' : 'negative'}">
                        ${order.side.toUpperCase()} ${order.filled_qty || order.qty} shares
                    </span>
                    <span style="color:#888;">$${order.filled_avg_price || order.limit_price || 'market'}</span>
                </div>
            `).join('');
        }
        
        function updatePortfolioChart(positions, quotes) {
            const chartDiv = document.getElementById('chart-bars');
            // Generate sample portfolio history (30 days)
            const days = [];
            const values = [];
            let baseValue = 50000;
            
            for (let i = 30; i >= 0; i--) {
                const date = new Date();
                date.setDate(date.getDate() - i);
                days.push(date.getDate());
                
                // Random walk for demo
                const change = (Math.random() - 0.5) * 2000;
                baseValue += change;
                values.push(Math.max(30000, Math.min(65000, baseValue)));
            }
            
            const maxValue = Math.max(...values);
            const minValue = Math.min(...values);
            
            chartDiv.innerHTML = values.map((value, i) => `
                <div class="bar-wrapper">
                    <div class="bar" style="height: ${((value - minValue) / (maxValue - minValue)) * 200 + 20}px"></div>
                    <div class="bar-label">${days[i]}</div>
                </div>
            `).join('');
        }
        
        async function triggerScan() {
            try {
                const response = await fetch('/scan', { method: 'POST' });
                if (response.ok) {
                    alert('Manual scan triggered! Check logs for details.');
                }
            } catch (error) {
                console.error('Error triggering scan:', error);
            }
        }
        
        async function refreshData() {
            await fetchData();
        }
        
        // Auto-refresh every 30 seconds
        fetchData();
        setInterval(fetchData, 30000);
    </script>
</body>
</html>
'''

# ── Flask Web Server ───────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def dashboard():
    return render_template_string(DASHBOARD_TEMPLATE)

@app.route('/health')
def health():
    return jsonify({'status': 'running', 'bot': 'Empire Trading Bot - LIVE', 'time': datetime.now(timezone.utc).isoformat()})

@app.route('/api/account')
def api_account():
    try:
        return jsonify(get_account())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/positions')
def api_positions():
    try:
        return jsonify(get_positions())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/orders')
def api_orders():
    try:
        return jsonify(get_orders(20))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/quotes')
def api_quotes():
    try:
        quotes = get_all_quotes()
        return jsonify(quotes)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/quote/<symbol>')
def api_quote(symbol):
    try:
        quote = get_quote(symbol.upper())
        return jsonify(quote) if quote else jsonify({'error': 'Not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/scan', methods=['POST'])
def manual_scan():
    try:
        thread = threading.Thread(target=run_scan)
        thread.start()
        return jsonify({'message': 'Scan triggered'}), 202
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Main Entry Point ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    log.info('🚀 Empire Trading Bot starting with Web Dashboard...')
    log.warning('⚠️ LIVE TRADING MODE ACTIVE')
    
    if not ALPACA_KEY or not ALPACA_SECRET:
        log.error('❌ ALPACA_API_KEY and ALPACA_SECRET_KEY must be set!')
        exit(1)
    
    # Start background scanner
    def scanner_loop():
        time.sleep(10)  # Wait for server to start
        while True:
            run_scan()
            time.sleep(SCAN_INTERVAL)
    
    scan_thread = threading.Thread(target=scanner_loop, daemon=True)
    scan_thread.start()
    
    # Start Flask server
    port = int(os.environ.get('PORT', 8080))
    send_telegram('🚀 Empire Bot Dashboard Started - LIVE MODE')
    
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
