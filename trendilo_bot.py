#!/usr/bin/env python3
"""
Trendilo -> Telegram (GitHub Actions, разовый запуск).
Проверяет сразу НЕСКОЛЬКО таймфреймов (по умолчанию 1min и 1h).
Токены берутся из переменных окружения (GitHub Secrets).

ВАЖНО: GitHub Actions не гарантирует запуск ровно раз в минуту — реальный
минимальный интервал около 5 минут (иногда с доп. задержкой на стороне GitHub).
Поэтому сигналы по таймфрейму 1min будут приходить с опозданием в несколько минут,
а не мгновенно. Для часового ТФ (1h) это не критично.
"""

import os
import json
import math
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]

SYMBOL = "XAU/USD"

# Список таймфреймов, которые проверяем за один запуск.
# Twelve Data принимает: 1min,5min,15min,30min,45min,1h,2h,4h,1day
INTERVALS = ["1min", "1h"]

SMOOTH = 1
LENGTH = 50
OFFSET = 0.85
SIGMA = 6
BMULT = 1.0
CUSTOM_BLEN = False
BLEN = 20

STATE_FILE = "state.json"


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=15)
    if not r.ok:
        print("Ошибка отправки в Telegram:", r.text)


def fetch_candles(interval, outputsize=300):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
        "order": "ASC",
    }
    r = requests.get(url, params=params, timeout=20)
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Ошибка API Twelve Data ({interval}): {data}")
    closes = [float(v["close"]) for v in data["values"]]
    times = [v["datetime"] for v in data["values"]]
    return times, closes


def alma_series(values, window, offset, sigma):
    n = len(values)
    result = [None] * n
    if sigma == 0:
        sigma = 1e-9
    m = offset * (window - 1)
    s = window / sigma
    weights = [math.exp(-((i - m) ** 2) / (2 * s * s)) for i in range(window)]
    wsum = sum(weights)
    for idx in range(window - 1, n):
        window_vals = values[idx - window + 1: idx + 1]
        acc = sum(w * v for w, v in zip(weights, window_vals))
        result[idx] = acc / wsum
    return result


def compute_cdir(times, closes):
    n = len(closes)
    pch = [None] * n
    for i in range(SMOOTH, n):
        pch[i] = (closes[i] - closes[i - SMOOTH]) / closes[i] * 100

    valid_start = SMOOTH
    pch_valid = pch[valid_start:]
    avpch_valid = alma_series(pch_valid, LENGTH, OFFSET, SIGMA)
    avpch = [None] * valid_start + avpch_valid

    blength = BLEN if CUSTOM_BLEN else LENGTH

    cdir = [None] * n
    rms = [None] * n
    for i in range(n):
        if avpch[i] is None:
            continue
        start = i - blength + 1
        if start < 0:
            continue
        window_avpch = avpch[start:i + 1]
        if any(v is None for v in window_avpch):
            continue
        mean_sq = sum(v * v for v in window_avpch) / blength
        r = BMULT * math.sqrt(mean_sq)
        rms[i] = r
        if avpch[i] > r:
            cdir[i] = 1
        elif avpch[i] < -r:
            cdir[i] = -1
        else:
            cdir[i] = 0

    return cdir, avpch, rms


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def label(cdir_val):
    return {1: "🟢 ЗЕЛЁНЫЙ (бычий)", -1: "🔴 КРАСНЫЙ (медвежий)", 0: "⚪ СЕРЫЙ (нейтральный)"}.get(cdir_val, "?")


def process_interval(interval, state):
    key = f"tf_{interval}"
    tf_state = state.get(key, {"last_time": None, "last_cdir": None})

    times, closes = fetch_candles(interval)
    cdir, avpch, rms = compute_cdir(times, closes)

    idx = len(cdir) - 2  # последний закрытый бар
    if idx < 0 or cdir[idx] is None:
        return

    last_time = times[idx]
    last_cdir = cdir[idx]

    if tf_state["last_time"] != last_time:
        if tf_state["last_cdir"] is not None and last_cdir != tf_state["last_cdir"]:
            msg = (
                f"Trendilo [{SYMBOL} {interval}]\n"
                f"Смена состояния: {label(tf_state['last_cdir'])} -> {label(last_cdir)}\n"
                f"Время бара: {last_time}\n"
                f"avpch={avpch[idx]:.4f}  rms={rms[idx]:.4f}"
            )
            print(msg)
            send_telegram(msg)
        tf_state["last_time"] = last_time
        tf_state["last_cdir"] = last_cdir
        state[key] = tf_state
    else:
        print(f"[{interval}] Бар без изменений: {last_time}, cdir={last_cdir}")


def main():
    state = load_state()
    for interval in INTERVALS:
        try:
            process_interval(interval, state)
        except Exception as e:
            print(f"Ошибка на таймфрейме {interval}:", e)
    save_state(state)


if __name__ == "__main__":
    main()
