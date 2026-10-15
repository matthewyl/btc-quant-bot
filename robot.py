import requests
import pandas as pd
import numpy as np
import os
from datetime import datetime, timezone
from math import log, sqrt, exp
from scipy.stats import norm  # GitHub Actions 已有 scipy（經 scikit-learn）

HISTORY_FILE = "historical_greeks.csv"

# ====================== Telegram ======================
def send_telegram_notification(message: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("⚠️ 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID（請設定 GitHub Secrets）")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            print("🚀 Telegram 推播成功")
        else:
            print(f"❌ Telegram 失敗: {res.status_code} | {res.text}")
    except Exception as e:
        print(f"⚠️ Telegram 發送錯誤: {e}")


# ====================== 簡易 Black-Scholes Delta ======================
def bs_delta(S, K, T, r, sigma, option_type="C"):
    """簡易 BS delta（r=0 對 crypto 足夠）"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (log(S / K) + (0.5 * sigma**2) * T) / (sigma * sqrt(T))
        if option_type == "C":
            return norm.cdf(d1)
        else:
            return norm.cdf(d1) - 1.0
    except Exception:
        return 0.0


# ====================== 真正 Deribit 數據 ======================
def fetch_deribit_options():
    """取得 BTC 所有未到期 options 的 book summary"""
    url = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
    params = {"currency": "BTC", "kind": "option"}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json().get("result", [])
        if not data:
            raise ValueError("Empty result from Deribit")
        print(f"✅ 成功取得 {len(data)} 個 BTC options 合約")
        return pd.DataFrame(data)
    except Exception as e:
        print(f"❌ Deribit API 失敗: {e}")
        return None


def parse_and_enrich(df: pd.DataFrame):
    """解析 instrument_name + 計算近似 delta + 過濾"""
    if df is None or df.empty:
        return None

    # 解析 BTC-24OCT26-95000-C 格式
    def parse_name(name):
        try:
            parts = name.split("-")
            if len(parts) != 4:
                return None, None, None, None
            expiry_str, strike, opt_type = parts[1], float(parts[2]), parts[3]
            # 轉換到期日
            expiry = datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc)
            return expiry, strike, opt_type, name
        except Exception:
            return None, None, None, None

    parsed = df["instrument_name"].apply(parse_name)
    df[["expiry", "strike", "opt_type", "name"]] = pd.DataFrame(parsed.tolist(), index=df.index)

    df = df.dropna(subset=["expiry", "strike", "opt_type"])
    df = df[df["open_interest"] > 0]  # 只保留有持倉

    # 取得現貨價（用最流動合約的 underlying_price）
    underlying = df["underlying_price"].median()
    if pd.isna(underlying) or underlying <= 0:
        underlying = df["underlying_price"].iloc[0]

    now = datetime.now(timezone.utc)
    df["T"] = df["expiry"].apply(lambda x: max((x - now).total_seconds() / (365.25 * 24 * 3600), 1/365.25))

    # 用 mark_iv 計算近似 delta
    df["mark_iv"] = df["mark_iv"].fillna(df["mark_iv"].median()) / 100.0  # 轉成小數
    df["delta"] = df.apply(
        lambda row: bs_delta(underlying, row["strike"], row["T"], 0.0, row["mark_iv"], row["opt_type"]),
        axis=1
    )

    # 近似 gamma（簡化）
    df["gamma"] = df.apply(
        lambda row: norm.pdf(
            (log(underlying / row["strike"]) + 0.5 * row["mark_iv"]**2 * row["T"]) /
            (row["mark_iv"] * sqrt(row["T"]))
        ) / (underlying * row["mark_iv"] * sqrt(row["T"])) if row["T"] > 0 and row["mark_iv"] > 0 else 0.0,
        axis=1
    )

    return df, underlying


def calculate_metrics(df: pd.DataFrame, underlying: float):
    """計算核心指標"""
    calls = df[df["opt_type"] == "C"]
    puts = df[df["opt_type"] == "P"]

    # 1. OI PCR
    call_oi = calls["open_interest"].sum()
    put_oi = puts["open_interest"].sum()
    pcr_oi = put_oi / call_oi if call_oi > 0 else 1.0

    # 2. Volume PCR
    call_vol = calls["volume"].sum()
    put_vol = puts["volume"].sum()
    pcr_vol = put_vol / call_vol if call_vol > 0 else 1.0

    # 3. 25-Delta Skew (Put IV - Call IV)  ← 正數 = put 較貴 = 偏空
    near_25d_put = puts[(puts["delta"].abs() >= 0.20) & (puts["delta"].abs() <= 0.30)]
    near_25d_call = calls[(calls["delta"] >= 0.20) & (calls["delta"] <= 0.30)]

    put_iv_25 = near_25d_put["mark_iv"].mean() * 100 if not near_25d_put.empty else np.nan
    call_iv_25 = near_25d_call["mark_iv"].mean() * 100 if not near_25d_call.empty else np.nan

    if not np.isnan(put_iv_25) and not np.isnan(call_iv_25):
        skew_25d = put_iv_25 - call_iv_25
    else:
        # fallback：用較寬的 delta 範圍
        skew_25d = 0.0

    # 4. Net Delta & Total Gamma（簡化代理）
    net_delta = df["delta"].mul(df["open_interest"]).sum() / 1000  # 縮放方便閱讀
    total_gamma = df["gamma"].mul(df["open_interest"]).sum()

    # 5. ATM IV（參考）
    atm = df[df["strike"].between(underlying * 0.98, underlying * 1.02)]
    atm_iv = atm["mark_iv"].mean() * 100 if not atm.empty else np.nan

    return {
        "skew_25d": round(skew_25d, 2),
        "pcr_oi": round(pcr_oi, 3),
        "pcr_vol": round(pcr_vol, 3),
        "net_delta": round(net_delta, 2),
        "total_gamma": round(total_gamma, 6),
        "atm_iv": round(atm_iv, 1) if not np.isnan(atm_iv) else None,
        "underlying": round(underlying, 1),
        "call_oi": int(call_oi),
        "put_oi": int(put_oi)
    }


def update_history_and_thresholds(metrics: dict):
    """更新歷史並計算動態閾值"""
    new_row = pd.DataFrame([{
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "skew": metrics["skew_25d"],
        "pcr": metrics["pcr_oi"],
        "gamma": metrics["total_gamma"],
        "delta": metrics["net_delta"]
    }])

    if os.path.exists(HISTORY_FILE):
        try:
            hist = pd.read_csv(HISTORY_FILE)
            hist = pd.concat([hist, new_row], ignore_index=True)
            hist = hist.drop_duplicates(subset=["date"], keep="last")
            # 只保留最近 60 日
            hist = hist.tail(60)
        except Exception:
            hist = new_row
    else:
        hist = new_row

    hist.to_csv(HISTORY_FILE, index=False)

    # 動態閾值（有足夠歷史先用 percentile，否則用合理預設）
    if len(hist) >= 10:
        skew_up = hist["skew"].quantile(0.80)
        skew_low = hist["skew"].quantile(0.20)
        pcr_up = hist["pcr"].quantile(0.75)
        pcr_low = hist["pcr"].quantile(0.25)
    else:
        skew_up, skew_low = 4.0, -1.5
        pcr_up, pcr_low = 1.15, 0.75

    return skew_up, skew_low, pcr_up, pcr_low


def generate_signal(metrics: dict, skew_up, skew_low, pcr_up, pcr_low):
    """簡單但有效的規則（可之後再加 ML）"""
    skew = metrics["skew_25d"]
    pcr = metrics["pcr_oi"]

    # 偏空：高 put skew + 高 PCR
    if skew > skew_up and pcr > pcr_up:
        up_prob = 32.0
        signal = "🔴 *強烈偏空*\n高 25Δ Skew + 高 OI PCR → 機構對沖/看跌需求強"
    # 偏多：低（甚至負）skew + 低 PCR
    elif skew < skew_low and pcr < pcr_low:
        up_prob = 68.0
        signal = "🟢 *強烈偏多*\n低 Skew + 低 PCR → 市場願意買 call / 少對沖"
    # 中性偏空
    elif skew > 2.5 or pcr > 1.1:
        up_prob = 42.0
        signal = "🟠 *輕微偏空*\nSkew 或 PCR 偏高，謹慎"
    # 中性偏多
    elif skew < 0.5 and pcr < 0.85:
        up_prob = 58.0
        signal = "🟢 *輕微偏多*\n籌碼面相對樂觀"
    else:
        up_prob = 50.0
        signal = "🟡 *中性觀望*\n指標處於常態區間，建議空倉或輕倉"

    return up_prob, signal


# ====================== Main ======================
if __name__ == "__main__":
    try:
        raw = fetch_deribit_options()
        if raw is None:
            raise ValueError("無法取得 Deribit 數據")

        df, underlying = parse_and_enrich(raw)
        if df is None or df.empty:
            raise ValueError("解析後無有效合約")

        metrics = calculate_metrics(df, underlying)
        skew_up, skew_low, pcr_up, pcr_low = update_history_and_thresholds(metrics)
        up_prob, signal = generate_signal(metrics, skew_up, skew_low, pcr_up, pcr_low)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M") + " HKT"

        msg = (
            f"🔮 *BTC Options Quant Report*\n"
            f"📅 {now_str}\n"
            f"-----------------------------------------\n"
            f"💰 *現貨價:* `${metrics['underlying']:,.0f}`\n"
            f"📊 *25Δ Skew (Put−Call):* `{metrics['skew_25d']:+.2f}` vol\n"
            f"   (動態區間: `{skew_low:.1f}` ~ `{skew_up:.1f}`)\n"
            f"📈 *OI PCR:* `{metrics['pcr_oi']:.3f}`  (Vol PCR: `{metrics['pcr_vol']:.3f}`)\n"
            f"📉 *Net Delta (代理):* `{metrics['net_delta']:.1f}`\n"
            f"⚡ *Total Gamma (代理):* `{metrics['total_gamma']:.5f}`\n"
            f"📉 *ATM IV:* `{metrics['atm_iv']}%`\n"
            f"📦 *Call OI / Put OI:* `{metrics['call_oi']:,}` / `{metrics['put_oi']:,}`\n"
            f"-----------------------------------------\n"
            f"🧠 *未來 7 天上行概率（規則版）:* `{up_prob}%`\n\n"
            f"⚙️ *執行建議:*\n{signal}\n"
            f"-----------------------------------------"
        )

        print(msg)
        send_telegram_notification(msg)

    except Exception as e:
        error_msg = f"❌ BTC Quant Bot 出錯: {str(e)}"
        print(error_msg)
        send_telegram_notification(error_msg)
