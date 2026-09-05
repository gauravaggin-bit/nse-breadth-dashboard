# NSE market breadth dashboard

Daily breadth for every NSE main-board stock, built only from NSE's official archive files:
advance/decline counts, A/D ratio and A/D line, new 52-week highs/lows (all stocks and
Nifty 50 members), Nifty 50 new-high days this month / this year, and the share of stocks
above their 20/50/200-day moving averages.

## One-time setup
1. Install Python 3.10+ and then:  `pip install pandas numpy requests`
2. Put `breadth_update.py` and `dashboard_template.html` in one folder.
3. Run `python breadth_update.py` once. The first run back-fills ~20 months of daily
   files (about 420 downloads, 5–10 minutes). Later runs take a few seconds.

## Every day
NSE publishes the bhavcopy around 18:30–19:30 IST. After that, run
`python breadth_update.py` and open `dashboard.html` in any browser. The page is fully
self-contained (no internet needed to view it), so it also works behind a corporate firewall.

To automate:
* Windows — Task Scheduler → Create Basic Task → daily 19:45 → Start a program:
  `python C:\path\to\breadth_update.py`
* macOS/Linux — `crontab -e` and add: `45 19 * * 1-5 cd /path/to/folder && python3 breadth_update.py`

## How the numbers are defined
| Metric | Definition |
|---|---|
| Advances / declines | Close vs NSE's previous close (already adjusted for corporate actions) |
| A/D ratio | advances ÷ declines |
| A/D line | Running sum of (advances − declines) from the start of stored history |
| New 52-week high / low | Today's high above (low below) the highest high (lowest low) of the prior 250 trading days; stock needs ≥200 days of history |
| Nifty 50 new-high days | Days the index *closed* above its prior 250-day high (52-wk) or above every stored prior close ("all-time", i.e. since history start) |
| % above N DMA | Share of stocks whose close is above their simple N-day average, among stocks with ≥N days of data |

Universe = `SctySrs == "EQ"` (normal rolling-settlement stocks). To include trade-for-trade
names, set `SERIES = {"EQ", "BE"}` in the config block. Other knobs (index name, windows,
chart length) are at the top of `breadth_update.py`.

## Files
* `data/bhav/` and `data/index/` — one small CSV per trading day (the raw cache; never re-downloaded)
* `data/no_data_days.txt` — dates NSE had no file (holidays), skipped on later runs
* `data/metrics.json` — the computed series, if you want to use them elsewhere (Excel, Sheets)
* `dashboard.html` — the page you open

## If a download fails
NSE occasionally blocks non-browser traffic. The script sends browser headers and retries
three times; if it still fails, wait a few minutes and rerun — the cache means it only
fetches what is missing.
