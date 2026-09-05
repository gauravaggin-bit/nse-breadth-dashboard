#!/usr/bin/env python3
"""
NSE market-breadth updater.

Run once a day after ~19:30 IST:   python breadth_update.py
First run back-fills history (needs ~250 trading days for 52-week highs
and 200 DMA), later runs only fetch the days that are missing.

Data sources (all official NSE archive files, no login needed):
  * Cash-market bhavcopy  -> every listed stock's OHLC + previous close
      https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
  * Index close file       -> Nifty 50 (and every other index) OHLC
      https://nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv
  * Nifty 50 constituents  -> to count index members at new highs
      https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv

Output: dashboard.html (self-contained, data embedded) + data/metrics.json
"""

import io
import json
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------- config ---
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BHAV_DIR = DATA / "bhav"
INDEX_DIR = DATA / "index"
TEMPLATE = ROOT / "dashboard_template.html"
OUTPUT = ROOT / "index.html"   # index.html so it also works as the GitHub Pages home page

BACKFILL_CALENDAR_DAYS = 620   # ~420 trading days -> ~170 days of charted history
SERIES = {"EQ"}                # main-board series; add "BE" to include trade-for-trade
INDEX_NAME = "Nifty 50"
WINDOW_52W = 250               # trading days
MIN_HISTORY_52W = 200          # a stock needs this much history to count for new high/low
DMA_PERIODS = (20, 50, 200)
CHART_DAYS = 400               # max days embedded in the dashboard
REQUEST_PAUSE = 0.6            # seconds between downloads (be polite to NSE)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

BHAV_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
INDEX_URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{dmy}.csv"
NIFTY50_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"


# ------------------------------------------------------------- download ---
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:                                   # warm up cookies; harmless if it fails
        s.get("https://www.nseindia.com/", timeout=10)
    except requests.RequestException:
        pass
    return s


def fetch(session: requests.Session, url: str) -> bytes | None:
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 404:
                return None                # holiday / not published yet
            if r.ok and len(r.content) > 200:
                return r.content
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def load_bhavcopy(session, d: date) -> pd.DataFrame | None:
    """Return DataFrame [symbol, high, low, close, prev_close] for main-board equities."""
    path = BHAV_DIR / f"{d:%Y-%m-%d}.csv"
    if path.exists():
        return pd.read_csv(path)
    raw = fetch(session, BHAV_URL.format(ymd=f"{d:%Y%m%d}"))
    if raw is None:
        return None
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
        df = pd.read_csv(z.open(name))
    df = df[(df["Sgmt"] == "CM") & (df["SctySrs"].isin(SERIES))]
    out = pd.DataFrame({
        "symbol": df["TckrSymb"].astype(str).str.strip(),
        "high": pd.to_numeric(df["HghPric"], errors="coerce"),
        "low": pd.to_numeric(df["LwPric"], errors="coerce"),
        "close": pd.to_numeric(df["ClsPric"], errors="coerce"),
        "prev_close": pd.to_numeric(df["PrvsClsgPric"], errors="coerce"),
    }).dropna(subset=["close"])
    out = out.drop_duplicates("symbol")
    out.to_csv(path, index=False)
    time.sleep(REQUEST_PAUSE)
    return out


def load_index(session, d: date) -> dict | None:
    """Return {open, high, low, close} for INDEX_NAME on date d."""
    path = INDEX_DIR / f"{d:%Y-%m-%d}.csv"
    if path.exists():
        df = pd.read_csv(path)
    else:
        raw = fetch(session, INDEX_URL.format(dmy=f"{d:%d%m%Y}"))
        if raw is None:
            return None
        df = pd.read_csv(io.BytesIO(raw))
        df.columns = [c.strip() for c in df.columns]
        df.to_csv(path, index=False)
        time.sleep(REQUEST_PAUSE)
    row = df[df["Index Name"].astype(str).str.strip().str.lower() == INDEX_NAME.lower()]
    if row.empty:
        return None
    r = row.iloc[0]
    return {k: float(r[c]) for k, c in
            (("open", "Open Index Value"), ("high", "High Index Value"),
             ("low", "Low Index Value"), ("close", "Closing Index Value"))}


def load_nifty50_members(session) -> set[str]:
    path = DATA / "nifty50_members.csv"
    raw = fetch(session, NIFTY50_URL)
    if raw is not None:
        path.write_bytes(raw)
    if not path.exists():
        return set()
    df = pd.read_csv(path)
    col = [c for c in df.columns if c.strip().lower() == "symbol"][0]
    return set(df[col].astype(str).str.strip())


def trading_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def sync(session) -> tuple[dict[date, pd.DataFrame], dict[date, dict]]:
    BHAV_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    holidays_file = DATA / "no_data_days.txt"
    skip = set(holidays_file.read_text().split()) if holidays_file.exists() else set()

    today = date.today()
    start = today - timedelta(days=BACKFILL_CALENDAR_DAYS)
    bhav, idx = {}, {}
    days = list(trading_days(start, today))
    for i, d in enumerate(days, 1):
        key = f"{d:%Y-%m-%d}"
        if key in skip:
            continue
        b = load_bhavcopy(session, d)
        if b is None:
            if d < today - timedelta(days=3):      # old & missing -> holiday, remember it
                skip.add(key)
            elif d == today:
                print("Today's bhavcopy is not published yet (NSE posts it ~18:30-19:30 IST).")
            continue
        bhav[d] = b
        ix = load_index(session, d)
        if ix:
            idx[d] = ix
        if i % 20 == 0 or i == len(days):
            print(f"  {i}/{len(days)} days checked, {len(bhav)} trading days loaded", flush=True)
    holidays_file.write_text("\n".join(sorted(skip)))
    return bhav, idx


# -------------------------------------------------------------- compute ---
def compute(bhav: dict[date, pd.DataFrame], idx: dict[date, dict], n50: set[str]) -> dict:
    dates = sorted(bhav)
    close = pd.DataFrame({d: bhav[d].set_index("symbol")["close"] for d in dates}).T
    high = pd.DataFrame({d: bhav[d].set_index("symbol")["high"] for d in dates}).T
    low = pd.DataFrame({d: bhav[d].set_index("symbol")["low"] for d in dates}).T
    prev = pd.DataFrame({d: bhav[d].set_index("symbol")["prev_close"] for d in dates}).T
    close.index = high.index = low.index = prev.index = pd.to_datetime(dates)

    adv = (close > prev).sum(axis=1)
    dec = (close < prev).sum(axis=1)
    unch = (close == prev).sum(axis=1)
    ad_ratio = adv / dec.replace(0, np.nan)
    ad_line = (adv - dec).cumsum()

    prior_max = high.shift(1).rolling(WINDOW_52W, min_periods=MIN_HISTORY_52W).max()
    prior_min = low.shift(1).rolling(WINDOW_52W, min_periods=MIN_HISTORY_52W).min()
    is_new_high = high > prior_max
    is_new_low = low < prior_min
    new_high = is_new_high.sum(axis=1)
    new_low = is_new_low.sum(axis=1)
    n50_cols = [c for c in close.columns if c in n50]
    n50_new_high = is_new_high[n50_cols].sum(axis=1) if n50_cols else pd.Series(0, index=close.index)
    n50_new_low = is_new_low[n50_cols].sum(axis=1) if n50_cols else pd.Series(0, index=close.index)

    pct_above = {}
    for p in DMA_PERIODS:
        sma = close.rolling(p, min_periods=p).mean()
        pct_above[p] = ((close > sma).sum(axis=1) / sma.notna().sum(axis=1).replace(0, np.nan) * 100)

    # ---- index
    ix = pd.DataFrame.from_dict({pd.Timestamp(d): v for d, v in idx.items()}, orient="index").sort_index()
    ix = ix.reindex(close.index).ffill()
    ix_prior_max = ix["close"].shift(1).rolling(WINDOW_52W, min_periods=MIN_HISTORY_52W).max()
    ix_new_52w = ix["close"] > ix_prior_max
    ix_new_ath = ix["close"] > ix["close"].shift(1).expanding().max()  # highest in stored history

    # ---- assemble
    valid = pct_above[200].notna()          # only publish days with full 200-DMA coverage
    first = valid.idxmax() if valid.any() else close.index[0]
    keep = close.index[close.index >= first][-CHART_DAYS:]

    def ser(s):
        return [None if pd.isna(v) else round(float(v), 2) for v in s.reindex(keep)]

    rows = {
        "dates": [d.strftime("%Y-%m-%d") for d in keep],
        "advances": ser(adv), "declines": ser(dec), "unchanged": ser(unch),
        "ad_ratio": ser(ad_ratio), "ad_line": ser(ad_line),
        "new_high": ser(new_high), "new_low": ser(new_low),
        "n50_new_high": ser(n50_new_high), "n50_new_low": ser(n50_new_low),
        "pct_above_20": ser(pct_above[20]), "pct_above_50": ser(pct_above[50]),
        "pct_above_200": ser(pct_above[200]),
        "index_close": ser(ix["close"]),
        "index_new_52w_high": [bool(v) for v in ix_new_52w.reindex(keep)],
        "index_new_ath": [bool(v) for v in ix_new_ath.reindex(keep)],
        "universe_size": ser(close.notna().sum(axis=1)),
    }

    last = keep[-1]
    mtd = keep[(keep.year == last.year) & (keep.month == last.month)]
    ytd = keep[keep.year == last.year]
    summary = {
        "as_of": last.strftime("%Y-%m-%d"),
        "index_name": INDEX_NAME,
        "index_close": rows["index_close"][-1],
        "index_change_pct": round(float(ix["close"].pct_change().reindex(keep).iloc[-1] * 100), 2)
        if len(keep) > 1 else None,
        "index_52w_high": round(float(ix["close"].reindex(keep).rolling(WINDOW_52W, min_periods=1).max().iloc[-1]), 2),
        "index_new_52w_high_days_mtd": int(ix_new_52w.reindex(mtd).sum()),
        "index_new_52w_high_days_ytd": int(ix_new_52w.reindex(ytd).sum()),
        "index_new_ath_days_mtd": int(ix_new_ath.reindex(mtd).sum()),
        "index_new_ath_days_ytd": int(ix_new_ath.reindex(ytd).sum()),
        "n50_new_high_today": int(n50_new_high.reindex(keep).iloc[-1]),
        "n50_new_high_mtd": int(n50_new_high.reindex(mtd).sum()),
        "n50_new_high_ytd": int(n50_new_high.reindex(ytd).sum()),
        "stock_new_high_mtd": int(new_high.reindex(mtd).sum()),
        "stock_new_high_ytd": int(new_high.reindex(ytd).sum()),
        "stock_new_low_mtd": int(new_low.reindex(mtd).sum()),
        "stock_new_low_ytd": int(new_low.reindex(ytd).sum()),
        "history_start": close.index[0].strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "n50_members": len(n50_cols),
    }
    # today's lists (handy for drill-down)
    lists = {
        "new_highs_today": sorted(is_new_high.loc[last][is_new_high.loc[last]].index.tolist()),
        "new_lows_today": sorted(is_new_low.loc[last][is_new_low.loc[last]].index.tolist()),
    }
    return {"summary": summary, "series": rows, "lists": lists}


# ------------------------------------------------------------------ main ---
def main():
    print("Syncing NSE archive files...")
    session = make_session()
    bhav, idx = sync(session)
    if len(bhav) < DMA_PERIODS[-1] + 5:
        sys.exit(f"Only {len(bhav)} trading days loaded; need > {DMA_PERIODS[-1]} for the 200 DMA. "
                 "Check network access to nsearchives.nseindia.com and rerun.")
    n50 = load_nifty50_members(session)
    print(f"Computing breadth on {len(bhav)} days, {len(n50)} Nifty 50 members...")
    result = compute(bhav, idx, n50)
    (DATA / "metrics.json").write_text(json.dumps(result))
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", json.dumps(result))
    OUTPUT.write_text(html, encoding="utf-8")
    s = result["summary"]
    print(f"Done. {s['as_of']}: A/D {result['series']['advances'][-1]}/{result['series']['declines'][-1]}, "
          f"new highs {result['series']['new_high'][-1]}, new lows {result['series']['new_low'][-1]}. "
          f"Open {OUTPUT.name}")


if __name__ == "__main__":
    main()
