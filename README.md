# Whale Feed bot (paper only)

Reads PUBLIC Hyperliquid data every ~5 min (GitHub Actions). No keys, no real orders.

- `bot.py` – polls 11 wallets, paper-copies big entries, rebuilds each wallet's 90-day position record (2 wallets per run, refreshed every 6h).
- `docs/index.html` – 5-tab page (feed, wallet records, paper ledger, high win-rate, gold). Reads only `docs/data.json`.
- `state.json` / `history.json` / `docs/data.json` – written by the bot.
- `config.json` – wallets and paper settings (margin, leverage, min size).
