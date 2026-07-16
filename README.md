# XAUUSD FVRP Signal Bot

GitHub Actions-powered bot that monitors XAUUSD (Gold), builds FVRP zones for **Asia**, **London**, and **New York** sessions, and sends breakout alerts via Telegram.

## Setup

1. Create a **new public GitHub repository** and push these files
2. Go to Settings → Secrets and variables → Actions → New repository secret
3. Add two secrets:
   - `TELEGRAM_TOKEN` — your bot token from [@BotFather](https://t.me/BotFather)
   - `TELEGRAM_CHAT_ID` — your chat ID from [@userinfobot](https://t.me/userinfobot)
4. The workflow runs automatically on schedule

## How It Works

| Session | Zone Build UTC | Active Trading |
|---------|---------------|----------------|
| Asia    | 00:00         | 00:00–London open |
| London  | 07:00–08:00*  | 07:00–NY open |
| New York| 13:30–14:30*  | 13:30–23:55 |

\*Auto-adjusts for DST (BST/GMT, EDT/EST)

- Runs every 30 min on weekdays
- Builds zone at session start (polls Swissquote for 15 min)
- Checks for breakouts between sessions
- Sends Telegram alert on breakout + trend confirmation
- State persists via git commits

## File Structure

```
├── .github/workflows/bot.yml   # GitHub Actions workflow
├── signal_bot.py               # Bot logic
├── requirements.txt            # Dependencies
└── state/                      # Persistent state (auto-committed)
    ├── zones.json
    ├── triggered.json
    └── price_history.json
```

## Models Monitored

- 2R (sl_atr=0.2, target=2.0) — recommended
- 2R-q (sl_atr=0.3)
- 2R-h (sl_atr=0.5)
- 1R (sl_atr=1.0)
- 1R-70 (sl_atr=1.5)
