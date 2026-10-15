import requests
import pandas as pd
import numpy as np
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from math import log, sqrt
from scipy.stats import norm

HISTORY_FILE = "historical_greeks.csv"

# ====================== Telegram 推播 ======================
def send_telegram_notification(message: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("⚠️ 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID（請至 GitHub Secrets 檢查）")
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


# ====================== 期權定價核心 (BS 模型) ======================
def bs_delta(S: float, K: float, T: float, r: float, sigma: float, option_type: str = "C") -> float:
    """計算標準 BS Delta（r 預設 0）"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt(T))
        if option_type == "C":
            return float(norm.cdf(d1))
        else:
            return float(norm.cdf(d1) - 1.0)
    except Exception:
        return 0.0


def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """計算標準 BS Gamma"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt(T))
        return float(norm.pdf(d1) / (S * sigma * sqrt(T)))
    except Exception:
        return 0.0


# ====================== 爬取與清洗 Deribit 數據 ======================
def fetch_deribit_options() -> pd.DataFrame:
    url = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
    params = {"currency": "BTC", "kind": "option"}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json().get("result", [])
        if not data:
            raise ValueError("Deribit 返回空數據")
        print(f"✅ 成功取得 {len(data)} 個 BTC options 合約")
        return pd.DataFrame(data)
    except Exception as e:
        print(f"❌ Deribit API 請求失敗: {e}")
        return None


def parse_and_enrich(df: pd.DataFrame):
    if df is None or df.empty:
        return None, None

    # 解析合約名稱：BTC-24OCT26-95000-C
    def parse_name(name):
        try:
            parts = name.split("-")
            if len(parts) != 4:
                return None, None, None, None
            # Deribit 官方標準每日 08:00 UTC 結算
            expiry = datetime.strptime(parts[1], "%d%b%y").replace(
                hour=8, minute=0, second=0, tzinfo=timezone.utc
            )
            return expiry, float(parts[2]), parts[3], name
        except Exception:
            return None, None, None, None

    parsed = df["instrument_name"].apply(parse_name)
    df[["expiry", "strike", "opt_type", "name"]] = pd.DataFrame(parsed.tolist(), index=df.index)
    df = df.dropna(subset=["expiry", "strike", "opt_type"])

    now = datetime.now(timezone.utc)
    # 過濾已到期合約與 0 持倉合約
    df = df[df["expiry"] > now]
    df = df[df["open_interest"] > 0]

    # 取底層資產現貨價
    underlying = df["underlying_price"].dropna().median()
    if pd.isna(underlying) or underlying <= 0:
        underlying = df["underlying_price"].iloc[0]

    # 年化到期時間 T
    df["T"] = df["expiry"].apply(lambda x: (x - now).total_seconds() / (365.25 * 86400))
    df["days_to_expiry"] = df["T"] * 365.25

    # 隱含波動率換算
    df["mark_iv"] = df["mark_iv"].fillna(df["mark_iv"].median()) / 100.0

    # 計算 Greeks
    df["delta"] = df.apply(
        lambda row: bs_delta(underlying, row["strike"], row["T"], 0.0, row["mark_iv"], row["opt_type"]),
        axis=1
    )
    df["gamma"] = df.apply(
        lambda row: bs_gamma(underlying, row["strike"], row["T"], 0.0, row["mark_iv"]),
        axis=1
    )

    return df, underlying


# ====================== 計算量化指標 ======================
def calculate_metrics(df: pd.DataFrame, underlying: float):
    # 1. 全市場 OI PCR 與 Volume PCR
    call_oi = df[df["opt_type"] == "C"]["open_interest"].sum()
    put_oi = df[df["opt_type"] == "P"]["open_interest"].sum()
    pcr_oi = put_oi / call_oi if call_oi > 0 else 1.0

    call_vol = df[df["opt_type"] == "C"]["volume"].sum()
    put_vol = df[df["opt_type"] == "P"]["volume"].sum()
    pcr_vol = put_vol / call_vol if call_vol > 0 else 1.0

    # 2. 期限結構對齊：選取最接近 7 天（短期核心）的特定到期日
    now = datetime.now(timezone.utc)
    unique_expiries = sorted(df["expiry"].unique())
    
    # 鎖定 5~14 天內最接近 7 天的到期日，若無則取最近的一個未到期日
    eligible_expiries = [exp for exp in unique_expiries if (exp - now).total_seconds() >= 2 * 86400]
    if not eligible_expiries:
        target_expiry = unique_expiries[0]
    else:
        target_expiry = min(
            eligible_expiries,
            key=lambda exp: abs((exp - now).total_seconds() - 7 * 86400)
        )

    target_df = df[df["expiry"] == target_expiry]
    target_dte = round((target_expiry - now).total_seconds() / 86400, 1)

    # 3. 目標期限的 25-Delta Skew
    calls_term = target_df[target_df["opt_type"] == "C"]
    puts_term = target_df[target_df["opt_type"] == "P"]

    near_25d_put = puts_term[(puts_term["delta"].abs() >= 0.20) & (puts_term["delta"].abs() <= 0.30)]
    near_25d_call = calls_term[(calls_term["delta"] >= 0.20) & (calls_term["delta"] <= 0.30)]

    put_iv = near_25d_put["mark_iv"].mean() * 100 if not near_25d_put.empty else np.nan
    call_iv = near_25d_call["mark_iv"].mean() * 100 if not near_25d_call.empty else np.nan

    if not np.isnan(put_iv) and not np.isnan(call_iv):
        skew_25d = put_iv - call_iv
    else:
        # Fallback: 放寬至 0.15 ~ 0.35
        fallback_puts = puts_term[(puts_term["delta"].abs() >= 0.15) & (puts_term["delta"].abs() <= 0.35)]
        fallback_calls = calls_term[(calls_term["delta"] >= 0.15) & (calls_term["delta"] <= 0.35)]
        p_iv = fallback_puts["mark_iv"].mean() * 100 if not fallback_puts.empty else np.nan
        c_iv = fallback_calls["mark_iv"].mean() * 100 if not fallback_calls.empty else np.nan
        skew_25d = (p_iv - c_iv) if (not np.isnan(p_iv) and not np.isnan(c_iv)) else 0.0

    # 4. 目標期限 ATM IV (行權價 ±2%)
    atm_term = target_df[target_df["strike"].between(underlying * 0.98, underlying * 1.02)]
    atm_iv = atm_term["mark_iv"].mean() * 100 if not atm_term.empty else np.nan

    # 5. 全市場未平倉 Delta 與總 Gamma 代理
    net_delta = df["delta"].mul(df["open_interest"]).sum() / 1000
    total_gamma = df["gamma"].mul(df["open_interest"]).sum()

    return {
        "skew_25d": round(skew_25d, 2),
        "target_dte": target_dte,
        "pcr_oi": round(pcr_oi, 3),
        "pcr_vol": round(pcr_vol, 3),
        "net_delta": round(net_delta, 2),
        "total_gamma": round(total_gamma, 5),
        "atm_iv": round(atm_iv, 1) if not np.isnan(atm_iv) else None,
        "underlying": round(underlying, 1),
        "call_oi": int(call_oi),
        "put_oi": int(put_oi)
    }


# ====================== 歷史存儲與動態分位數 ======================
def update_history_and_thresholds(metrics: dict):
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
            hist = hist.tail(60)
        except Exception:
            hist = new_row
    else:
        hist = new_row

    hist.to_csv(HISTORY_FILE, index=False)

    if len(hist) >= 10:
        skew_up = hist["skew"].quantile(0.80)
        skew_low = hist["skew"].quantile(0.20)
        pcr_up = hist["pcr"].quantile(0.75)
        pcr_low = hist["pcr"].quantile(0.25)
    else:
        skew_up, skew_low = 3.5, -1.0
        pcr_up, pcr_low = 1.05, 0.70

    return skew_up, skew_low, pcr_up, pcr_low


# ====================== 多空信號評估 ======================
def generate_signal(metrics: dict, skew_up, skew_low, pcr_up, pcr_low):
    skew = metrics["skew_25d"]
    pcr = metrics["pcr_oi"]

    # 綜合評分：高 Skew (看跌保護貴) 與高 PCR 扣分；低 Skew 與低 PCR 加分
    # 偏空：Put 溢價高 + PCR 結構沉重
    if skew > skew_up and pcr > pcr_up:
        score = 25
        signal = "🔴 *強烈偏空*\n高 25Δ Skew 與高 OI PCR 同步共振，市場避險需求強烈。"
    elif skew < skew_low and pcr < pcr_low:
        score = 80
        signal = "🟢 *強烈偏多*\n低 Skew（Call 買盤強）疊加低 PCR，多頭持倉佔據主導。"
    elif skew > 2.0 and pcr > 0.9:
        score = 40
        signal = "🟠 *輕微偏空*\nPut 避險情緒偏濃，上方承壓。"
    elif skew < 0.5 and pcr < 0.75:
        score = 65
        signal = "🟢 *輕微偏多*\n市場下行對沖意願較低，籌碼面結構良好。"
    else:
        score = 50
        signal = "🟡 *中性盤整*\n期權隱含情緒處於中樞區間，無明顯單邊方向引導。"

    return score, signal


# ====================== 主程式 ======================
if __name__ == "__main__":
    try:
        raw = fetch_deribit_options()
        if raw is None:
            raise ValueError("無法取得 Deribit 數據")

        df, underlying = parse_and_enrich(raw)
        if df is None or df.empty:
            raise ValueError("合約解析後為空")

        metrics = calculate_metrics(df, underlying)
        skew_up, skew_low, pcr_up, pcr_low = update_history_and_thresholds(metrics)
        score, signal = generate_signal(metrics, skew_up, skew_low, pcr_up, pcr_low)

        # 鎖定香港時間輸出
        now_hkt = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M") + " HKT"

        msg = (
            f"🔮 *BTC Options Quant Report*\n"
            f"📅 {now_hkt}\n"
            f"-----------------------------------------\n"
            f"💰 *現貨價:* `${metrics['underlying']:,.0f}`\n"
            f"📊 *25Δ Skew ({metrics['target_dte']}D 到期):* `{metrics['skew_25d']:+.2f}` vol\n"
            f"   (動態區間: `{skew_low:.1f}` ~ `{skew_up:.1f}`)\n"
            f"📈 *OI PCR:* `{metrics['pcr_oi']:.3f}`  (Vol PCR: `{metrics['pcr_vol']:.3f}`)\n"
            f"📉 *Net Delta (代理):* `{metrics['net_delta']:.1f}`\n"
            f"⚡ *Total Gamma (代理):* `{metrics['total_gamma']:.5f}`\n"
            f"📉 *ATM IV ({metrics['target_dte']}D):* `{metrics['atm_iv']}%`\n"
            f"📦 *Call OI / Put OI:* `{metrics['call_oi']:,}` / `{metrics['put_oi']:,}`\n"
            f"-----------------------------------------\n"
            f"🧠 *短期多空評分:* `{score} / 100`\n\n"
            f"⚙️ *執行建議:*\n{signal}\n"
            f"-----------------------------------------"
        )

        print(msg)
        send_telegram_notification(msg)

    except Exception as e:
        error_msg = f"❌ BTC Quant Bot 出錯: {str(e)}"
        print(error_msg)
        send_telegram_notification(error_msg)
