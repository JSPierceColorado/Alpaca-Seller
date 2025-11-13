# Alpaca Position Manager & Trailing Exit Bot (Google Sheets + alpaca-trade-api)

This script monitors your **open positions on Alpaca** and maintains an **"Alpaca-Trader"** worksheet in your `Active-Investing` Google Sheet. It:

* Tracks per-position **% gain**, **all-time-high % gain**, and **armed** status.
* Applies **hard stop-loss** and **trailing take-profit** rules.
* Sells positions that hit the rules and logs them in a **Closed Trades** section.
* Runs in an infinite loop, updating once per minute.

> ⚠️ **Warning:** This script calls `alpaca.close_position()` and can **sell real positions**. Use with caution, and test in paper trading first.

---

## 🧾 High-Level Behavior

On each cycle (once per minute):

1. Ensures the Google Sheet structure is correct.
2. Reads existing active position data from the `Alpaca-Trader` worksheet.
3. Fetches current open positions from Alpaca via `alpaca_trade_api.REST`.
4. For each open position:

   * Computes **percent gain**.
   * Loads the saved **all-time-high % gain (ATH)** and **armed flag** from the sheet, if present.
   * Updates ATH if the new gain exceeds the previous ATH.
   * Arms the position once the gain hits **+5%**.
   * Applies exit rules:

     * **Rule 1:** Hard stop-loss at **-3%**.
     * **Rule 2:** Trailing take profit: if **armed** and current % gain is **3% or more below ATH**, sell.
5. If a sell is triggered, calls `alpaca.close_position(ticker)` and logs the trade in the **Closed Trades** section.
6. Writes the updated active positions back into the sheet.

This loop repeats every **60 seconds**.

---

## 📊 Google Sheets Layout

Spreadsheet: **`Active-Investing`**
Worksheet: **`Alpaca-Trader`** (created automatically if missing)

### Active Positions (Columns A–H)

Header row (row 1, columns A–H):

| Col | Header                 | Description                                      |
| --- | ---------------------- | ------------------------------------------------ |
| A   | `Ticker`               | Symbol (e.g. `AAPL`)                             |
| B   | `Qty`                  | Position quantity                                |
| C   | `Cost Basis`           | Average entry price                              |
| D   | `Current Price`        | Latest price from Alpaca                         |
| E   | `% Gain`               | Current percentage gain from cost basis          |
| F   | `All-Time High % Gain` | Highest % gain seen so far for this run/position |
| G   | `Armed?`               | `TRUE` if trailing take profit is armed          |
| H   | `Last Updated`         | ISO-8601 UTC timestamp                           |

Rows 2+ are used for **active positions**. At each cycle, the script clears `A2:H500` and writes the updated active positions.

### Closed Trades (Columns J onward)

Closed trades are logged starting at **J1** with header:

| Col | Header          | Description                                |
| --- | --------------- | ------------------------------------------ |
| J   | `Closed Trades` | Section label                              |
| K   | `Ticker`        | Symbol closed                              |
| L   | `% Gain/Loss`   | % gain/loss at close                       |
| M   | `Armed?`        | Whether the trade was armed when it closed |
| N   | `Closed At`     | ISO-8601 UTC timestamp                     |

Each closed trade is appended as a new row using `append_row(..., table_range="J1")`.

---

## 🔐 Environment Variables

The script expects the following environment variables:

| Variable            | Required | Description                                                              |
| ------------------- | -------- | ------------------------------------------------------------------------ |
| `GOOGLE_CREDS_JSON` | Yes      | Service account JSON for Google Sheets/Drive, stored as a single string. |
| `ALPACA_API_KEY`    | Yes      | Alpaca API key.                                                          |
| `ALPACA_API_SECRET` | Yes      | Alpaca API secret.                                                       |

### `GOOGLE_CREDS_JSON` Format

`GOOGLE_CREDS_JSON` should contain the full service account JSON (the same content you’d normally store in a `credentials.json` file). The script currently does:

```python
creds_json = os.environ.get("GOOGLE_CREDS_JSON")
creds_dict = eval(creds_json)
```

So make sure the env var content is valid Python/JSON-like dict literal, e.g.:

```bash
export GOOGLE_CREDS_JSON='{"type":"service_account","project_id":"...","private_key_id":"...","private_key":"-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n","client_email":"...","client_id":"...","auth_uri":"...","token_uri":"...","auth_provider_x509_cert_url":"...","client_x509_cert_url":"..."}'
```

> 🔒 **Note:** Using `eval` on environment input can be risky; consider refactoring to `json.loads` for production.

---

## 🔌 Alpaca Connection

The script uses the legacy `alpaca_trade_api` client:

```python
from alpaca_trade_api import REST

API_KEY = os.environ.get("ALPACA_API_KEY")
API_SECRET = os.environ.get("ALPACA_API_SECRET")
APCA_API_BASE_URL = "https://api.alpaca.markets"

alpaca = REST(API_KEY, API_SECRET, APCA_API_BASE_URL)
```

Key calls:

* `alpaca.list_positions()` – fetches all open positions.
* `alpaca.close_position(ticker)` – closes a single position at market.

---

## 📦 Installation

You’ll need:

* Python 3.9+ (recommended)
* Access to Alpaca (live or paper trading)
* A Google Cloud service account with Sheets & Drive access

Install dependencies (basic):

```bash
pip install pandas gspread oauth2client alpaca-trade-api
```

---

## ▶️ Running the Bot

After setting environment variables, run:

```bash
python alpaca_trader_loop.py
```

The main block:

```python
if __name__ == "__main__":
    ws = connect_sheet()
    while True:
        try:
            run_cycle(ws)
        except Exception as e:
            print("Error during cycle:", e)
        time.sleep(60)
```

This will:

* Connect to the sheet
* Perform a full update/sell cycle
* Sleep 60 seconds
* Repeat indefinitely

You’ll see console output like:

```text
SOLD AAPL
SOLD MSFT
Error during cycle: <some transient error>
```

---

## 📈 Exit Rules Summary

For each open position:

* **Hard Stop-Loss:**

  * If `% Gain <= -3%` → **sell immediately**.

* **Trailing Take Profit:**

  * If `% Gain >= +5%` → `Armed? = TRUE`.
  * If **armed** and `% Gain <= (ATH - 3%)` → **sell**.

ATH (`All-Time High % Gain`) is tracked per position in the sheet and updated whenever the current gain exceeds the previous ATH.

Closed trades are recorded with:

* Ticker
* Final % Gain/Loss at close
* Armed status
* Timestamp

---

## ⚠️ Safety & Suggestions

* Run this in **paper trading** first by pointing to Alpaca’s paper endpoint and using paper API keys.
* Consider adding:

  * Logging to a file or Google Sheet tab for audit
  * Limits on total positions / daily turnover
  * Email/Slack/Discord alerts on sells
* Ensure your `GOOGLE_CREDS_JSON` and API keys are stored securely (e.g. in a secret manager).

---

## 📄 License

Add your preferred license text here (MIT, Apache 2.0, etc.).
