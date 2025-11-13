import os
import time
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from alpaca_trade_api import REST
from datetime import datetime

# ----------------------------------------------------------------------
# Google Sheets Setup
# ----------------------------------------------------------------------
def connect_sheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_CREDS_JSON env variable missing")

    # NOTE: assumes GOOGLE_CREDS_JSON is a literal dict string in env
    creds_dict = eval(creds_json)

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
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
APCA_API_BASE_URL = "https://api.alpaca.markets"

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
        # Only compare the left portion (A1:H1) to avoid nuking formatting
        if existing[:len(sheet_header)] != sheet_header:
            ws.update(
                values=[sheet_header],
                range_name="A1:H1"
            )
    except Exception as e:
        print("Error ensuring main header:", e)
        ws.update(
            values=[sheet_header],
            range_name="A1:H1"
        )

    # Section header for closed trades in J1:N1
    closed_header = ["Closed Trades", "Ticker", "% Gain/Loss", "Armed?", "Closed At"]
    try:
        # Column 10 == "J"
        if ws.cell(1, 10).value != "Closed Trades":
            ws.update(
                values=[closed_header],
                range_name="J1:N1"
            )
    except Exception as e:
        print("Error ensuring closed-trades header:", e)
        ws.update(
            values=[closed_header],
            range_name="J1:N1"
        )


# ----------------------------------------------------------------------
# Load active tracker data from sheet (A1:H)
# ----------------------------------------------------------------------
def load_active(ws):
    # Only look at A1:H500 to avoid duplicate header issues
    values = ws.get_values("A1:H500")

    # If absolutely nothing or only header
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

    # Google Sheets may return rows shorter than header, pad them
    padded_rows = [row + [""] * (len(header) - len(row)) for row in rows]

    df = pd.DataFrame(padded_rows, columns=header)
    return df


# ----------------------------------------------------------------------
# Log a closed trade (J table)
# ----------------------------------------------------------------------
def record_closed_trade(ws, ticker, gain, armed):
    row = ["", ticker, gain, armed, datetime.utcnow().isoformat()]
    ws.append_row(row, table_range="J1")


# ----------------------------------------------------------------------
# Main trading loop logic
# ----------------------------------------------------------------------
def run_cycle(ws):
    ensure_sheet_structure(ws)
    df = load_active(ws)

    positions = alpaca.list_positions()
    active_symbols = [pos.symbol for pos in positions]

    results = []

    for pos in positions:
        ticker = pos.symbol
        qty = float(pos.qty)
        cost = float(pos.avg_entry_price)
        current = float(pos.current_price)
        percent_gain = (current - cost) / cost * 100

        # fetch saved ATH + armed status
        if not df.empty and ticker in df["Ticker"].values:
            row = df[df["Ticker"] == ticker].iloc[0]
            ath_val = row.get("All-Time High % Gain", "")
            ath = float(ath_val) if ath_val != "" else percent_gain
            armed = str(row.get("Armed?", "")).upper() == "TRUE"
        else:
            ath = percent_gain
            armed = False

        # update ATH
        if percent_gain > ath:
            ath = percent_gain

        # arming logic
        if percent_gain >= 5 and not armed:
            armed = True

        # selling logic
        should_sell = False

        # Rule 1: hard stop-loss at -3%
        if percent_gain <= -3:
            should_sell = True

        # Rule 2: trailing take profit: if armed + drop 3% from ATH
        if armed and percent_gain <= (ath - 3):
            should_sell = True

        if should_sell:
            try:
                alpaca.close_position(ticker)
                print(f"SOLD {ticker}")

                record_closed_trade(ws, ticker, round(percent_gain, 2), armed)

            except Exception as e:
                print("Sell error:", e)
            continue

        # keep active
        results.append([
            ticker, qty, cost, current,
            round(percent_gain, 2), round(ath, 2),
            "TRUE" if armed else "FALSE",
            datetime.utcnow().isoformat()
        ])

    # update sheet: clear A2:H500 then write fresh results
    ws.update(
        range_name="A2:H500",
        values=[[""] * 8] * 499  # rows 2–500 inclusive
    )
    if results:
        ws.update(
            range_name="A2",
            values=results
        )


# ----------------------------------------------------------------------
# LOOP FOREVER
# ----------------------------------------------------------------------
if __name__ == "__main__":
    ws = connect_sheet()
    while True:
        try:
            run_cycle(ws)
        except Exception as e:
            print("Error during cycle:", e)
        time.sleep(60)
