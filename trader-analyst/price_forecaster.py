"""Calibrated 10m / 30m / 60m price forecaster with walk-forward accuracy and live scoring.

Why this exists
---------------
The Monte Carlo in ``intraday_estimator`` gives probabilities, but its "expected price"
comes from a drift that is never checked against what really happened. This module
produces a point price forecast for each horizon that is *fitted and validated* on the
coin's own recent 1-minute history:

1. Features are computed at every past minute using only data available at that minute
   (order-flow imbalance, EMA trend, short momentum, distance from rolling VWAP,
   volatility-normalised).
2. A ridge regression maps features -> forward log return for each horizon.
3. Walk-forward test: fit on the first 70% of history, predict the last 30%.
   The out-of-sample error decides how much of the model's signal we keep
   (optimal shrinkage); if the model does not beat "price stays where it is",
   the forecast collapses to the current price instead of inventing a move.
4. Ranges come from the empirical out-of-sample residuals, not a normal assumption.
5. Every live forecast is stored and scored when its horizon matures, so the
   dashboard reports the forecaster's real, realised error.

Standard library only.
"""

import json
import math
import os
import threading
import time

HORIZONS = (("10m", 10), ("30m", 30), ("60m", 60))
HISTORY_LIMIT = 1000          # 1m bars per Binance request
HISTORY_PAGES = 3             # 3 x 1000 = ~50 hours of 1m bars used for fitting
MODEL_TTL_SEC = 60.0          # refit at most once per minute per symbol
RIDGE_LAMBDA = 3.0
TRAIN_FRACTION = 0.70
VWAP_WINDOW = 60
TRACK_INTERVAL_SEC = 60.0     # store at most one live forecast per symbol per minute
MAX_LOG_PER_HORIZON = 500

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKER_PATH = os.path.join(_ROOT, "trader", "price_forecast_tracker.json")

_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()
_TRACK_LOCK = threading.Lock()
_TRACKER = None


# ---------------------------------------------------------------------------
# Feature engineering (strictly causal)
# ---------------------------------------------------------------------------

def _ema(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def _feature_rows(klines):
    """Return list of (index, feature_vector) for every bar with enough look-back."""
    n = len(klines)
    if n < VWAP_WINDOW + 5:
        return []
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    qvol = [float(k[7]) if len(k) > 7 else float(k[5]) * float(k[4]) for k in klines]
    tbq = [float(k[10]) if len(k) > 10 else 0.5 * qv for k, qv in zip(klines, qvol)]
    vol = [float(k[5]) for k in klines]
    logret = [0.0] + [math.log(closes[i] / closes[i - 1]) if closes[i - 1] > 0 and closes[i] > 0 else 0.0
                      for i in range(1, n)]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)

    rows = []
    for t in range(VWAP_WINDOW, n):
        win = logret[t - 29:t + 1]
        mu = sum(win) / len(win)
        sig = math.sqrt(sum((r - mu) ** 2 for r in win) / len(win)) or 1e-6
        p = closes[t]
        q10 = sum(qvol[t - 9:t + 1])
        flow = (sum(tbq[t - 9:t + 1]) / q10 - 0.5) if q10 > 0 else 0.0
        trend = ((ema9[t] - ema21[t]) / p) / sig
        mom = (math.log(p / closes[t - 10]) if closes[t - 10] > 0 else 0.0) / (sig * math.sqrt(10))
        pv = sum(((highs[i] + lows[i] + closes[i]) / 3.0) * vol[i] for i in range(t - VWAP_WINDOW + 1, t + 1))
        vv = sum(vol[t - VWAP_WINDOW + 1:t + 1])
        vwap = pv / vv if vv > 0 else p
        dist = (math.log(p / vwap) if vwap > 0 else 0.0) / (sig * math.sqrt(VWAP_WINDOW))
        rng = (highs[t] - lows[t]) / p / sig if p > 0 else 0.0
        clip = lambda x: max(-4.0, min(4.0, x))  # noqa: E731
        rows.append((t, [1.0, clip(flow * 10.0), clip(trend), clip(mom), clip(dist), clip(rng - 1.5)], sig))
    return rows


# ---------------------------------------------------------------------------
# Ridge regression (closed form, Gaussian elimination)
# ---------------------------------------------------------------------------

def _solve(a, b):
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            return [0.0] * n
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def _ridge_fit(xs, ys, lam=RIDGE_LAMBDA):
    d = len(xs[0])
    xtx = [[0.0] * d for _ in range(d)]
    xty = [0.0] * d
    for x, y in zip(xs, ys):
        for i in range(d):
            xty[i] += x[i] * y
            xi = x[i]
            row = xtx[i]
            for j in range(d):
                row[j] += xi * x[j]
    for i in range(d):
        xtx[i][i] += lam * len(xs) / 100.0
    xtx[0][0] += lam * len(xs) / 50.0  # Regularise intercept to prevent past 50h drift leakage
    return _solve(xtx, xty)


def _dot(w, x):
    return sum(a * b for a, b in zip(w, x))


def _quantile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(len(sorted_vals) - 1, lo + 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


# ---------------------------------------------------------------------------
# Model fitting with walk-forward validation
# ---------------------------------------------------------------------------

def fit_models(history):
    """Fit one validated model per horizon from 1m klines. Returns dict keyed by horizon."""
    rows = _feature_rows(history)
    closes = [float(k[4]) for k in history]
    models = {}
    for name, h in HORIZONS:
        # Targets are expressed in units of local volatility so calm and wild periods weigh equally.
        samples = [(x, math.log(closes[t + h] / closes[t]) / (sig * math.sqrt(h)), sig)
                   for t, x, sig in rows if t + h < len(closes) and closes[t] > 0 and closes[t + h] > 0]
        if len(samples) < 120:
            models[name] = None
            continue
        split = int(len(samples) * TRAIN_FRACTION)
        # Leave a gap of h bars between train and test so overlapping targets do not leak.
        train = samples[:max(30, split - h)]
        test = samples[split:]
        w_tr = _ridge_fit([s[0] for s in train], [s[1] for s in train])

        preds = [_dot(w_tr, s[0]) for s in test]
        ys = [s[1] for s in test]
        num = sum(p * y for p, y in zip(preds, ys))
        den = sum(p * p for p in preds)
        shrink = max(0.0, min(1.0, num / den)) if den > 1e-12 else 0.0

        # Out-of-sample errors in real return units (percent).
        err_model, err_naive, resid, hits, hit_n = [], [], [], 0, 0
        for (x, y_n, sig), p in zip(test, preds):
            scale = sig * math.sqrt(h)
            y_ret = y_n * scale
            f_ret = shrink * p * scale
            err_model.append(abs(y_ret - f_ret))
            err_naive.append(abs(y_ret))
            resid.append((y_ret - f_ret) / scale)
            if abs(f_ret) > 1e-9 and abs(y_ret) > 1e-9:
                hit_n += 1
                hits += 1 if (f_ret > 0) == (y_ret > 0) else 0
        mae_model = sum(err_model) / len(err_model)
        mae_naive = sum(err_naive) / len(err_naive)
        skill = 1.0 - (mae_model / mae_naive) if mae_naive > 0 else 0.0
        if skill <= 0.0:
            # Model is not better than "no change" out of sample: do not trust its direction.
            shrink = 0.0
            mae_model = mae_naive
            resid = [s[1] for s in test]

        w_all = _ridge_fit([s[0] for s in samples], [s[1] for s in samples])
        resid.sort()
        models[name] = {
            "weights": w_all,
            "shrink": round(shrink, 4),
            "oos_mae_pct": round(mae_model * 100.0, 4),
            "naive_mae_pct": round(mae_naive * 100.0, 4),
            "skill_vs_naive_pct": round(max(0.0, skill) * 100.0, 2),
            "oos_direction_hit_pct": round(100.0 * hits / hit_n, 1) if hit_n and shrink > 0 else None,
            "test_samples": len(test),
            "resid_q": {q: _quantile(resid, q) for q in (0.10, 0.25, 0.50, 0.75, 0.90)},
        }
    return models


def _get_models(bsym, fetch_history):
    now = time.time()
    with _MODEL_LOCK:
        c = _MODEL_CACHE.get(bsym)
        if c and now - c["ts"] < MODEL_TTL_SEC:
            return c["models"]
    history = fetch_history(bsym)
    models = fit_models(history) if history else {}
    with _MODEL_LOCK:
        _MODEL_CACHE[bsym] = {"ts": now, "models": models}
    return models


# ---------------------------------------------------------------------------
# Live forecast
# ---------------------------------------------------------------------------

def _prec(price):
    return 6 if price < 1.0 else (4 if price < 100.0 else 2)


def forecast_prices(bsym, k1m_recent, price, fetch_history, mc_horizons=None):
    """Return {horizon: price forecast dict} using the validated per-horizon models."""
    models = _get_models(bsym, fetch_history)
    rows = _feature_rows(k1m_recent)
    if not rows or price <= 0:
        return {}
    _, x_now, sig_now = rows[-1]
    prec = _prec(price)
    out = {}
    for name, h in HORIZONS:
        m = models.get(name) if models else None
        scale = sig_now * math.sqrt(h)
        if m:
            z = m["shrink"] * _dot(m["weights"], x_now)
            q = m["resid_q"]
            method = "walk-forward ridge (validated)" if m["shrink"] > 0 else "no-change (model had no out-of-sample edge)"
        else:
            # Not enough history: fall back to Monte Carlo median with empirical vol bands.
            mc = (mc_horizons or {}).get(name) or {}
            z = (math.log(mc["expected_price"] / price) / scale) if mc.get("expected_price") and scale > 0 else 0.0
            q = {0.10: -1.2816, 0.25: -0.6745, 0.50: 0.0, 0.75: 0.6745, 0.90: 1.2816}
            method = "monte-carlo fallback"
        ret = z * scale
        # Centre on the model forecast itself; residual quantiles only shape the band
        # (re-centred on their median so a trend in the test window cannot bias the target).
        med = q[0.50]
        q = {k: v - med for k, v in q.items()}
        center = price * math.exp(ret)
        mae_pct = m["oos_mae_pct"] if m else round(scale * 0.7979 * 100.0, 4)
        out[name] = {
            "horizon": name,
            "minutes": h,
            "predicted_price": round(center, prec),
            "predicted_move_pct": round((center / price - 1.0) * 100.0, 3),
            "likely_low": round(price * math.exp(ret + q[0.25] * scale), prec),
            "likely_high": round(price * math.exp(ret + q[0.75] * scale), prec),
            "range_80_low": round(price * math.exp(ret + q[0.10] * scale), prec),
            "range_80_high": round(price * math.exp(ret + q[0.90] * scale), prec),
            "typical_error_pct": round(mae_pct, 3),
            "typical_error_usd": round(price * mae_pct / 100.0, prec),
            "skill_vs_naive_pct": m["skill_vs_naive_pct"] if m else 0.0,
            "oos_direction_hit_pct": m["oos_direction_hit_pct"] if m else None,
            "test_samples": m["test_samples"] if m else 0,
            "method": method,
        }
    record_and_score(bsym, price, out)
    for name, _ in HORIZONS:
        if name in out:
            out[name]["live_accuracy"] = live_accuracy(bsym, name)
    return out


# ---------------------------------------------------------------------------
# Live realised-accuracy tracker
# ---------------------------------------------------------------------------

def _load_tracker():
    global _TRACKER
    if _TRACKER is None:
        try:
            with open(TRACKER_PATH, "r", encoding="utf-8") as fh:
                _TRACKER = json.load(fh)
        except Exception:
            _TRACKER = {}
        _TRACKER.setdefault("pending", [])
        _TRACKER.setdefault("scored", {})
        _TRACKER.setdefault("last_record", {})
    return _TRACKER


def _save_tracker():
    try:
        os.makedirs(os.path.dirname(TRACKER_PATH), exist_ok=True)
        tmp = TRACKER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_TRACKER, fh)
        os.replace(tmp, TRACKER_PATH)
    except Exception:
        pass


def record_and_score(bsym, price, forecasts, now=None):
    """Score matured forecasts for this symbol against the live price, then record new ones."""
    now = now if now is not None else time.time()
    with _TRACK_LOCK:
        tr = _load_tracker()
        changed = False
        keep = []
        for p in tr["pending"]:
            if p["sym"] == bsym and now >= p["due"]:
                # Only score if we are observing close to the due time (avoids stale scoring after downtime).
                if now - p["due"] <= 90.0:
                    err_pct = abs(price / p["pred"] - 1.0) * 100.0
                    naive_pct = abs(price / p["base"] - 1.0) * 100.0
                    moved = price - p["base"]
                    called = p["pred"] - p["base"]
                    key = "%s|%s" % (bsym, p["hz"])
                    log = tr["scored"].setdefault(key, [])
                    log.append([round(err_pct, 4), round(naive_pct, 4),
                                1 if (abs(called) > 0 and moved * called > 0) else (0 if abs(called) > 0 else -1),
                                1 if p["lo"] <= price <= p["hi"] else 0])
                    if len(log) > MAX_LOG_PER_HORIZON:
                        del log[:len(log) - MAX_LOG_PER_HORIZON]
                changed = True
            else:
                keep.append(p)
        tr["pending"] = keep

        if now - tr["last_record"].get(bsym, 0) >= TRACK_INTERVAL_SEC:
            for name, f in forecasts.items():
                tr["pending"].append({
                    "sym": bsym, "hz": name, "due": now + f["minutes"] * 60.0,
                    "base": price, "pred": f["predicted_price"],
                    "lo": f["range_80_low"], "hi": f["range_80_high"],
                })
            tr["last_record"][bsym] = now
            changed = True
        if changed:
            _save_tracker()


def live_accuracy(bsym, horizon):
    with _TRACK_LOCK:
        tr = _load_tracker()
        log = tr["scored"].get("%s|%s" % (bsym, horizon)) or []
    if not log:
        return {"scored": 0}
    n = len(log)
    mae = sum(r[0] for r in log) / n
    naive = sum(r[1] for r in log) / n
    dir_calls = [r[2] for r in log if r[2] >= 0]
    return {
        "scored": n,
        "mae_pct": round(mae, 3),
        "naive_mae_pct": round(naive, 3),
        "beats_naive": mae < naive,
        "direction_hit_pct": round(100.0 * sum(dir_calls) / len(dir_calls), 1) if dir_calls else None,
        "range_80_coverage_pct": round(100.0 * sum(r[3] for r in log) / n, 1),
    }
