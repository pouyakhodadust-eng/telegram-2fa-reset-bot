## Telegram Account 2FA Reset Bot

This project is a Telegram bot for account resellers that:

- Logs into pre-purchased Telegram accounts using SMS codes.
- Detects two-step verification (2FA) status.
- Attempts to reset 2FA (using Telegram's official API).
- Classifies each account into one of five categories and writes them to separate `.txt` files.
- Enforces a whitelist-based access system and per-user SOCKS5 proxy lists.

### Features

- **Async processing** via `asyncio` with concurrency equal to the number of configured proxies.
- **Per-user isolation** for proxies and statistics (stored in SQLite).
- **SOCKS5-only proxies**, rotated one proxy per concurrent worker.
- **Robust error handling** for network and API issues.

### Requirements

- Python 3.10+ recommended.
- A Telegram Bot token (from `@BotFather`).
- Telegram API ID and API Hash (see below).

Install dependencies:

```bash
pip install -r requirements.txt
```

### Configuration

Create a `.env` file in the project root with:

```bash
BOT_TOKEN=123456:ABC-DEF...
API_ID=2040
API_HASH=b18441a1ff607e10a989891a5462e627
MAIN_ADMIN_ID=123456789  # your Telegram numeric user ID
DB_PATH=bot.db           # optional, default is bot.db
```

> **Important — API credentials and SMS delivery:**
> Since February 2023, Telegram only sends login codes via SMS for
> requests made by official client API credentials. Custom API IDs
> (from `my.telegram.org`) will only receive in-app codes, which
> breaks SMS-based verification flows. The values above are the
> official **Telegram Desktop** credentials (`API_ID=2040`) and are
> recommended for this bot to work correctly with SMS providers.

### Running the Bot

```bash
python main.py
```

Then, in Telegram:

- The **main admin** (configured via `MAIN_ADMIN_ID`) can whitelist users and manage access.
- Whitelisted users can:
  - Manage their SOCKS5 proxies.
  - Upload `.txt` files of `+phone----sms_api_url` entries to process accounts.

