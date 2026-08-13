import csv
import json
import os
from collections import defaultdict

DATA_DIR = os.environ.get("DATA_DIR", ".")

rows = list(csv.DictReader(open(os.path.join(DATA_DIR, "weekly_top10.csv"))))
for r in rows:
    r["value_sgd_m"] = float(r["value_sgd_m"])
    r["rank"] = int(r["rank"])

weeks_sorted = sorted(set(r["week_start"] for r in rows))
week_index = {w: i for i, w in enumerate(weeks_sorted)}

by_ticker = defaultdict(list)
for r in rows:
    by_ticker[r["stock_code"]].append(r)

by_week_ticker = defaultdict(dict)
for r in rows:
    by_week_ticker[(r["week_start"], r["stock_code"])][r["side"]] = r["direction"]

divergence_events = []
for (week, ticker), sides in by_week_ticker.items():
    if "institutional" in sides and "retail" in sides:
        if sides["institutional"] != sides["retail"]:
            divergence_events.append({"week_start": week, "stock_code": ticker,
                                       "institutional": sides["institutional"], "retail": sides["retail"]})

ticker_summary = {}
for code, recs in by_ticker.items():
    recs_sorted = sorted(recs, key=lambda r: r["week_start"])
    name = recs_sorted[-1]["stock_name"]
    first_seen = recs_sorted[0]["week_start"]
    last_seen = recs_sorted[-1]["week_start"]

    streaks = {}
    for side in ("institutional", "retail"):
        for direction in ("buy", "sell"):
            weeks_present = sorted(set(r["week_start"] for r in recs if r["side"] == side and r["direction"] == direction),
                                    key=lambda w: week_index[w])
            weeks_present_idx = set(week_index[w] for w in weeks_present)
            if weeks_present_idx:
                cur = max(weeks_present_idx)
                streak = 1
                while (cur - 1) in weeks_present_idx:
                    streak += 1
                    cur -= 1
                streaks[f"{side}_{direction}"] = {"streak": streak, "last_week": weeks_sorted[max(weeks_present_idx)]}

    best_streak_key = max(streaks, key=lambda k: streaks[k]["streak"]) if streaks else None

    trend_notes = []
    for side in ("institutional", "retail"):
        for direction in ("buy", "sell"):
            key = f"{side}_{direction}"
            s = streaks.get(key)
            if s and s["streak"] >= 3 and s["last_week"] == weeks_sorted[-1]:
                trend_notes.append(
                    f"{side.title()} {direction} streak: {s['streak']} consecutive tracked weeks "
                    f"(through {s['last_week']}) — a multi-week trend is a more reliable signal than one week alone."
                )

    counts = defaultdict(int)
    vals = defaultdict(list)
    for r in recs:
        key = f"{r['side']}_{r['direction']}"
        counts[key] += 1
        vals[key].append(r["value_sgd_m"])

    my_divergences = [d for d in divergence_events if d["stock_code"] == code]

    ticker_summary[code] = {
        "code": code,
        "name": name,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "total_appearances": len(recs),
        "counts": dict(counts),
        "avg_value": {k: round(sum(v) / len(v), 2) for k, v in vals.items()},
        "max_abs_value": {k: round(max(v, key=abs), 2) for k, v in vals.items()},
        "streaks": streaks,
        "best_streak": {"type": best_streak_key, **streaks[best_streak_key]} if best_streak_key else None,
        "divergence_count": len(my_divergences),
        "recent_divergences": sorted(my_divergences, key=lambda d: d["week_start"])[-5:],
        "trend_notes": trend_notes,
    }

sector_rows = list(csv.DictReader(open(os.path.join(DATA_DIR, "sector_flow.csv"))))
for r in sector_rows:
    for k in list(r.keys()):
        if k not in ("week_date", "investor_type"):
            r[k] = float(r[k]) if r[k] not in (None, "") else None

daily_momentum_path = os.path.join(DATA_DIR, "daily_momentum.csv")
daily_momentum_rows = []
if os.path.exists(daily_momentum_path):
    daily_momentum_rows = list(csv.DictReader(open(daily_momentum_path)))

yahoo_prices_path = os.path.join(DATA_DIR, "yahoo_weekly_prices.csv")
yahoo_price_rows = []
if os.path.exists(yahoo_prices_path):
    yahoo_price_rows = list(csv.DictReader(open(yahoo_prices_path)))
    for r in yahoo_price_rows:
        r["close"] = float(r["close"])
        r["volume"] = int(r["volume"])

# --- Shares outstanding (for market-cap normalization) ---
shares_path = os.path.join(DATA_DIR, "shares_outstanding.csv")
shares_outstanding = {}
if os.path.exists(shares_path):
    for r in csv.DictReader(open(shares_path)):
        try:
            shares_outstanding[r["stock_code"]] = float(r["shares_outstanding"])
        except (ValueError, KeyError):
            pass

# --- Short-sell data (best-effort, third-party sourced) ---
short_sell_path = os.path.join(DATA_DIR, "short_sell.csv")
short_sell_rows = []
if os.path.exists(short_sell_path):
    short_sell_rows = list(csv.DictReader(open(short_sell_path)))
    for r in short_sell_rows:
        for k in ("short_sell_volume_000", "short_sell_value_sgd_000", "short_sell_pct"):
            if r.get(k) not in (None, ""):
                try:
                    r[k] = float(r[k])
                except ValueError:
                    r[k] = None

# --- Substantial shareholder disclosures ---
shareholder_path = os.path.join(DATA_DIR, "shareholder_announcements.csv")
shareholder_rows = []
if os.path.exists(shareholder_path):
    shareholder_rows = list(csv.DictReader(open(shareholder_path)))

# --- Company announcements (all other SGXNet categories) ---
# Capped per ticker: the dashboard shows the most recent few with a "show more"
# control, so shipping every historical filing would bloat data.json for rows
# nobody scrolls to. Newest first.
ANNOUNCEMENTS_PER_TICKER = 40
company_annc_path = os.path.join(DATA_DIR, "company_announcements.csv")
company_annc_rows = []
if os.path.exists(company_annc_path):
    _all_annc = list(csv.DictReader(open(company_annc_path)))
    _by_code = defaultdict(list)
    for r in _all_annc:
        # Titles arrive as "Category::Subject" — the prefix duplicates the
        # category column, so keep only the subject for display.
        title = (r.get("title") or "").strip()
        if "::" in title:
            title = title.split("::", 1)[1].strip()
        _by_code[r["stock_code"]].append({
            "stock_code": r["stock_code"],
            "date": r.get("date", ""),
            "category": (r.get("category") or "").strip(),
            "title": title,
            "url": r.get("url", ""),
        })
    for code, recs in _by_code.items():
        recs.sort(key=lambda x: x["date"], reverse=True)
        company_annc_rows.extend(recs[:ANNOUNCEMENTS_PER_TICKER])

# --- Reported quarterly results (Yahoo structured financials, matched to the
# SGX results filing that reported them) + forward analyst estimates.
# Both are best-effort: Yahoo covers ~59% of tracked tickers for financials and
# ~54% for estimates, so the dashboard must degrade gracefully when absent.
results_path = os.path.join(DATA_DIR, "results_summaries.csv")
results_rows = []
if os.path.exists(results_path):
    results_rows = list(csv.DictReader(open(results_path)))

forecasts_path = os.path.join(DATA_DIR, "analyst_forecasts.csv")
forecast_rows = []
if os.path.exists(forecasts_path):
    forecast_rows = list(csv.DictReader(open(forecasts_path)))

# About a third of covered tickers report in something other than SGD, and a
# forecast can exist for a ticker that has no recent reported quarter, so the
# currency has to come from its own lookup rather than from the results row.
currency_path = os.path.join(DATA_DIR, "reporting_currency.csv")
reporting_currency = {}
if os.path.exists(currency_path):
    for r in csv.DictReader(open(currency_path)):
        if r.get("stock_code") and r.get("currency"):
            reporting_currency[r["stock_code"]] = r["currency"]

# --- Rolling 4wk/12wk flow + price/flow signal classifier ---
flow_by_ticker_side = defaultdict(dict)  # (code, side) -> {week: value_sgd_m}
for r in rows:
    flow_by_ticker_side[(r["stock_code"], r["side"])][r["week_start"]] = r["value_sgd_m"]

price_by_ticker_week = {}
for r in yahoo_price_rows:
    price_by_ticker_week[(r["stock_code"], r["week_start"])] = r["close"]


def rolling_sum(code, side, week_idx, window):
    total, covered = 0.0, 0
    start = max(0, week_idx - window + 1)
    for i in range(start, week_idx + 1):
        v = flow_by_ticker_side.get((code, side), {}).get(weeks_sorted[i])
        if v is not None:
            total += v
            covered += 1
    return round(total, 2), covered, (week_idx - start + 1)


def price_change_pct(code, week_idx):
    if week_idx <= 0:
        return None
    p_cur = price_by_ticker_week.get((code, weeks_sorted[week_idx]))
    p_prev = price_by_ticker_week.get((code, weeks_sorted[week_idx - 1]))
    if p_cur is None or p_prev is None or p_prev == 0:
        return None
    return round((p_cur - p_prev) / p_prev * 100, 2)


SIGNAL_DEFINITIONS = {
    "strong_accumulation": {"label": "Strong accumulation", "tone": "positive",
                             "desc": "Institutional buying with price rising."},
    "quiet_accumulation": {"label": "Quiet accumulation", "tone": "neutral",
                            "desc": "Institutional buying while price holds flat — possible support building."},
    "retail_absorbing": {"label": "Retail absorbing", "tone": "neutral",
                          "desc": "Retail buying while institutions sell — retail may be absorbing institutional supply."},
    "distribution": {"label": "Distribution", "tone": "negative",
                      "desc": "Institutional selling with price falling — weak sentiment."},
    "dip_buying": {"label": "Possible dip-buying", "tone": "neutral",
                   "desc": "Retail buying after a sharp price drop — not always bullish."},
    "no_clear_signal": {"label": "No clear signal", "tone": "neutral",
                         "desc": "No dominant pattern this week."},
}


def classify_signal(inst_flow, retail_flow, price_chg):
    FLAT, DROP = 1.0, 3.0
    inst_dir = "buy" if (inst_flow or 0) > 0 else ("sell" if (inst_flow or 0) < 0 else None)
    retail_dir = "buy" if (retail_flow or 0) > 0 else ("sell" if (retail_flow or 0) < 0 else None)
    price_dir = None
    if price_chg is not None:
        price_dir = "rising" if price_chg > FLAT else ("falling" if price_chg < -FLAT else "flat")

    if inst_dir == "buy" and price_dir == "rising":
        return "strong_accumulation"
    if inst_dir == "buy" and price_dir == "flat":
        return "quiet_accumulation"
    if inst_dir == "sell" and retail_dir == "buy":
        return "retail_absorbing"
    if inst_dir == "sell" and price_dir == "falling":
        return "distribution"
    if retail_dir == "buy" and price_chg is not None and price_chg < -DROP:
        return "dip_buying"
    return "no_clear_signal"


ticker_signals = defaultdict(list)
for code in by_ticker.keys():
    mcap_shares = shares_outstanding.get(code)
    for week_idx, w in enumerate(weeks_sorted):
        inst_flow = flow_by_ticker_side.get((code, "institutional"), {}).get(w)
        retail_flow = flow_by_ticker_side.get((code, "retail"), {}).get(w)
        if inst_flow is None and retail_flow is None:
            continue
        price_chg = price_change_pct(code, week_idx)
        inst_4wk, inst_4cov, inst_4win = rolling_sum(code, "institutional", week_idx, 4)
        inst_12wk, inst_12cov, inst_12win = rolling_sum(code, "institutional", week_idx, 12)
        retail_4wk, retail_4cov, _ = rolling_sum(code, "retail", week_idx, 4)
        retail_12wk, retail_12cov, _ = rolling_sum(code, "retail", week_idx, 12)
        signal = classify_signal(inst_flow, retail_flow, price_chg)

        mcap_pct_inst, mcap_pct_retail = None, None
        close_this_week = price_by_ticker_week.get((code, w))
        if mcap_shares and close_this_week:
            mcap_sgd_m = mcap_shares * close_this_week / 1_000_000
            if mcap_sgd_m > 0:
                if inst_flow is not None:
                    mcap_pct_inst = round(inst_flow / mcap_sgd_m * 100, 3)
                if retail_flow is not None:
                    mcap_pct_retail = round(retail_flow / mcap_sgd_m * 100, 3)

        ticker_signals[code].append({
            "week_start": w,
            "institutional_flow": inst_flow,
            "retail_flow": retail_flow,
            "price_chg_pct": price_chg,
            "inst_4wk": inst_4wk, "inst_4wk_coverage": inst_4cov, "inst_4wk_window": inst_4win,
            "inst_12wk": inst_12wk, "inst_12wk_coverage": inst_12cov, "inst_12wk_window": inst_12win,
            "retail_4wk": retail_4wk, "retail_4wk_coverage": retail_4cov,
            "retail_12wk": retail_12wk, "retail_12wk_coverage": retail_12cov,
            "institutional_pct_mcap": mcap_pct_inst,
            "retail_pct_mcap": mcap_pct_retail,
            "signal": signal,
        })

out = {
    "weeks": weeks_sorted,
    "records": rows,
    "ticker_summary": ticker_summary,
    "divergence_events": sorted(divergence_events, key=lambda d: d["week_start"]),
    "sector_flow": sector_rows,
    "daily_momentum": daily_momentum_rows,
    "weekly_prices": yahoo_price_rows,
    "shares_outstanding": shares_outstanding,
    "signals": dict(ticker_signals),
    "signal_definitions": SIGNAL_DEFINITIONS,
    "short_sell": short_sell_rows,
    "shareholder_announcements": shareholder_rows,
    "company_announcements": company_annc_rows,
    "results_summaries": results_rows,
    "analyst_forecasts": forecast_rows,
    "reporting_currency": reporting_currency,
}

with open(os.path.join(DATA_DIR, "dashboard_data.json"), "w") as f:
    json.dump(out, f)

print(f"weeks={len(weeks_sorted)} tickers={len(ticker_summary)} divergence_events={len(divergence_events)} "
      f"yahoo_price_rows={len(yahoo_price_rows)} shares_outstanding={len(shares_outstanding)} "
      f"signal_weeks={sum(len(v) for v in ticker_signals.values())} short_sell_rows={len(short_sell_rows)} "
      f"shareholder_rows={len(shareholder_rows)} company_announcements={len(company_annc_rows)} "
      f"results_rows={len(results_rows)} forecast_rows={len(forecast_rows)}")
