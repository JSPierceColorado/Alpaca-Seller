import os
import time
import json
import ast
import re
from datetime import datetime, timezone

import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from alpaca_trade_api import REST

# ----------------------------------------------------------------------
# Helpers: env parsing
# ----------------------------------------------------------------------
def get_env_float(env_name: str, default: float) -> float:
    """
    Read env var as float with fallback.
    Tries the exact name and an uppercase variant.
    """
    raw = os.environ.get(env_name)
    if raw is None:
        raw = os.environ.get(env_name.upper())

    if raw is None:
        return float(default)

    try:
        return float(raw)
    except ValueError:
        print(f"Invalid {env_name} env value '{raw}', falling back to {default}")
        return float(default)


# Base thresholds (stocks/default)
STOP_LOSS_PCT = get_env_float("STOP_LOSS_PCT", -3.0)
ARMED_GAIN_PCT = get_env_float("ARMED_GAIN_PCT", 5.0)
TRAIL_DROP_PCT = get_env_float("TRAIL_DROP_PCT", 3.0)

# Options thresholds (default to base values if not provided)
OPTION_STOP_LOSS_PCT = get_env_float("Option_STOP_LOSS_PCT", STOP_LOSS_PCT)
OPTION_ARMED_GAIN_PCT = get_env_float("Option_ARMED_GAIN_PCT", ARMED_GAIN_PCT)
OPTION_TRAIL_DROP_PCT = get_env_float("Option_TRAIL_DROP_PCT", TRAIL_DROP_PCT)


# ----------------------------------------------------------------------
# Helpers: option detection + per-position thresholds
# ----------------------------------------------------------------------
# Alpaca options symbols are typically OCC-like: AAPL250117C00150000
_OCC_OPTION_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")

def looks_like_option_symbol(symbol: str) -> bool:
    return bool(_OCC_OPTION_RE.match(symbol or ""))

def thresholds_for_position(pos):
    """
    Returns (stop_loss_pct, armed_gain_pct, trail_drop_pct, is_option)
    using option thresholds if this is an options position.
    """
    symbol = str(getattr(pos, "symbol", "") or "")
    asset_class = str(getattr(pos, "asset_class", "") or "").lower()

    is_option = (asset_class == "us_option") or looks_like_option_symbol(symbol)

    if is_option:
        return OPTION_STOP_LOSS_PCT, OPTION_ARMED_GAIN_PCT, OPTION_TRAIL_DROP_PCT, True
    return STOP_LOSS_PCT, ARMED_GAIN_PCT, TRAIL_DROP_PCT, False


# ----------------------------------------------------------------------
# Google Sheets Setup
# ----------------------------------------------------------------------
def parse_google_creds(creds_raw: str) -> dict:
    """
    Supports either:
      - JSON string (recommended), or
      - Python dict literal string (legacy)
    """
    # Try JSON first
    try:
        return json.loads(creds_raw)
    except Exception:
        pass

    # Fall back to safe literal-eval (NOT eval)
    try:
        parsed = ast.literal_eval(creds_raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    raise ValueError("GOOGLE_CREDS_JSON must be valid JSON or a Python dict literal string")


def connect_sheet():
    creds_raw = os.environ.get("GOOGLE_CREDS_JSON")
    if not creds_raw:
        raise ValueError("GOOGLE_CREDS_JSON env variable missing")

    creds_dict = parse_google_creds(creds_raw)

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)

    sheet = client.open("Active-Investing")
    try:
        ws = sheet.worksheet("Alpaca-Trader")
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title="Alpaca-Trader", rows=500, cols=20)

    return ws


# ----------------------------------------------------------------------
# Alpaca Setup
# ----------------------------------------------------------------------
API_KEY = os.environ.get("ALPACA_API_KEY")
API_SECRET = os.environ.get("ALPACA_API_SECRET")
APCA_API_BASE_URL = os.environ.get("APCA_API_BASE_URL", "https://api.alpaca.markets")

if not API_KEY or not API_SECRET:
    raise ValueError("ALPACA_API_KEY / ALPACA_API_SECRET missing")

alpaca = REST(API_KEY, API_SECRET, APCA_API_BASE_URL)


# ----------------------------------------------------------------------
# Ensure Alpaca-Trader sheet has correct structure
# ----------------------------------------------------------------------
def ensure_sheet_structure(ws):
    # Main active positions header in A1:H1
    sheet_header = [
        "Ticker", "Qty", "Cost Basis", "Current Price",
        "% Gain", "All-Time High % Gain", "Armed?", "Last Updated"
    ]

    try:
        existing = ws.row_values(1)
        if existing[:len(sheet_header)] != sheet_header:
            ws.update(values=[sheet_header], range_name="A1:H1")
    except Exception as e:
        print("Error ensuring main header:", e)
        ws.update(values=[sheet_header], range_name="A1:H1")

    # Section header for closed trades in J1:N1
    closed_header = ["Closed Trades", "Ticker", "% Gain/Loss", "Armed?", "Closed At"]
    try:
        if ws.cell(1, 10).value != "Closed Trades":  # col J
            ws.update(values=[closed_header], range_name="J1:N1")
    except Exception as e:
        print("Error ensuring closed-trades header:", e)
        ws.update(values=[closed_header], range_name="J1:N1")


# ----------------------------------------------------------------------
# Load active tracker data from sheet (A1:H)
# ----------------------------------------------------------------------
def load_active(ws):
    values = ws.get_values("A1:H500")

    # If nothing or only header
    if not values or len(values) < 2:
        return pd.DataFrame(columns=[
            "Ticker", "Qty", "Cost Basis", "Current Price",
            "% Gain", "All-Time High % Gain", "Armed?", "Last Updated"
        ])

    header = values[0]
    rows = values[1:]

    # Strip out completely empty rows
    rows = [row for row in rows if any(cell != "" for cell in row)]
    if not rows:
        return pd.DataFrame(columns=header)

    # Pad rows to header length
    padded_rows = [row + [""] * (len(header) - len(row)) for row in rows]
    return pd.DataFrame(padded_rows, columns=header)


def safe_float(val, fallback=None):
    if val is None:
        return fallback
    s = str(val).strip()
    if s == "":
        return fallback
    try:
        return float(s)
    except ValueError:
        return fallback


# ----------------------------------------------------------------------
# Log a closed trade (J table)
# ----------------------------------------------------------------------
def record_closed_trade(ws, ticker, gain, armed):
    row = ["", ticker, gain, armed, datetime.now(timezone.utc).isoformat()]
    ws.append_row(row, table_range="J1")


# ----------------------------------------------------------------------
# Main trading loop logic
# ----------------------------------------------------------------------
def run_cycle(ws):
    ensure_sheet_structure(ws)
    df = load_active(ws)

    positions = alpaca.list_positions()
    results = []

    for pos in positions:
        ticker = str(pos.symbol)

        # Pick thresholds based on whether this position is an option
        stop_loss_pct, armed_gain_pct, trail_drop_pct, is_option = thresholds_for_position(pos)

        # Parse core numbers
        qty = safe_float(getattr(pos, "qty", None), 0.0) or 0.0
        cost = safe_float(getattr(pos, "avg_entry_price", None), None)
        current = safe_float(getattr(pos, "current_price", None), None)

        # If we can't price it, skip updating logic for safety (avoid accidental sells)
        if cost is None or current is None or cost == 0:
            print(f"Skipping {ticker}: missing/invalid pricing (avg_entry_price={cost}, current_price={current})")
            continue

        # Percent gain: handle short positions safely
        side = str(getattr(pos, "side", "long") or "long").lower()
        if side == "short":
            percent_gain = (cost - current) / cost * 100.0
        else:
            percent_gain = (current - cost) / cost * 100.0

        # Fetch saved ATH + armed status from sheet
        ath = percent_gain
        armed = False

        if not df.empty and "Ticker" in df.columns and ticker in df["Ticker"].values:
            row = df[df["Ticker"] == ticker].iloc[0]
            ath_val = row.get("All-Time High % Gain", "")
            armed_val = row.get("Armed?", "")

            saved_ath = safe_float(ath_val, None)
            if saved_ath is not None:
                ath = saved_ath

            armed = str(armed_val).strip().upper() == "TRUE"

        # If position goes negative, reset ATH and disarm
        if percent_gain < 0:
            ath = percent_gain
            armed = False
        else:
            # Update ATH
            if percent_gain > ath:
                ath = percent_gain

            # Arm if ATH has ever reached the threshold
            if ath >= armed_gain_pct:
                armed = True

        # Selling logic
        should_sell = False

        # Rule 1: hard stop-loss
        if percent_gain <= stop_loss_pct:
            should_sell = True

        # Rule 2: trailing take profit (only if armed)
        if armed and percent_gain <= (ath - trail_drop_pct):
            should_sell = True

        if should_sell:
            try:
                alpaca.close_position(ticker)
                print(
                    f"SOLD {ticker} ({'OPTION' if is_option else 'STOCK'}) @ {round(percent_gain, 2)}% | "
                    f"STOP_LOSS={stop_loss_pct}, ARMED_GAIN={armed_gain_pct}, TRAIL_DROP={trail_drop_pct}"
                )
                record_closed_trade(ws, ticker, round(percent_gain, 2), "TRUE" if armed else "FALSE")
            except Exception as e:
                print(f"Sell error for {ticker}:", e)
            continue

        # Keep active
        results.append([
            ticker,
            qty,
            cost,
            current,
            round(percent_gain, 2),
            round(ath, 2),
            "TRUE" if armed else "FALSE",
            datetime.now(timezone.utc).isoformat(),
        ])

    # Update sheet: clear A2:H500 then write fresh results
    ws.update(range_name="A2:H500", values=[[""] * 8] * 499)  # rows 2–500 inclusive
    if results:
        ws.update(range_name="A2", values=results)


# ----------------------------------------------------------------------
# LOOP FOREVER
# ----------------------------------------------------------------------
if __name__ == "__main__":
    print(
        "Thresholds:\n"
        f"  STOCK  : STOP_LOSS_PCT={STOP_LOSS_PCT}, ARMED_GAIN_PCT={ARMED_GAIN_PCT}, TRAIL_DROP_PCT={TRAIL_DROP_PCT}\n"
        f"  OPTION : Option_STOP_LOSS_PCT={OPTION_STOP_LOSS_PCT}, "
        f"Option_ARMED_GAIN_PCT={OPTION_ARMED_GAIN_PCT}, "
        f"Option_TRAIL_DROP_PCT={OPTION_TRAIL_DROP_PCT}\n"
        f"API BASE : {APCA_API_BASE_URL}"
    )

    ws = connect_sheet()

    while True:
        try:
            run_cycle(ws)
        except Exception as e:
            print("Error during cycle:", e)
        time.sleep(60)
