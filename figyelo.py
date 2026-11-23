#!/usr/bin/env python3
"""
CFD figyelő, GitHub Actions változat: US Tech 100 (Nasdaq-100).

Egyszeri futás: lekéri az árat, kiértékeli a szabályt, és ha új jelzés van,
értesítést küld a telefonra (ntfy). A GitHub ötpercenként újraindítja.

Beállítások környezeti változóból jönnek (GitHub Secrets), nincsenek a kódban:
  NTFY_TEMA   kötelező  a ntfy téma neve (nehezen kitalálható legyen)
  EGYENLEG    nem köt.  számlaegyenleg, pl. 5000
  KOCKAZAT    nem köt.  % ügyletenként, alapból 1
  FX          nem köt.  1 USD = ? számlapénznem (USD-s számlán 1)
  SPREAD      nem köt.  spread ár egységben, alapból 1
  TICKER      nem köt.  alapból ^NDX (index); NQ=F a határidős termék

Nem befektetési tanács és nem előrejelzés. A program nem kereskedik.
"""

import json
import math
import os
import sys
import time
import urllib.request

import pandas as pd

P = {"gyors": 20, "lassu": 50, "rsi": 14, "atr": 14, "atr_szorzo": 2.0, "rr": 2.0, "friss": 5}
INTERVALLUM = "15m"
PERIODUS = "30d"
PERC = 15
LEV = 20
LEPES = 0.01
ALLAPOT_FAJL = "state.json"


def env_szam(nev, alap):
    try:
        return float(os.environ.get(nev, "").replace(",", ".")) if os.environ.get(nev) else alap
    except ValueError:
        return alap


def letolt(ticker):
    import yfinance as yf

    utolso_hiba = None
    for _ in range(3):
        try:
            df = yf.download(ticker, period=PERIODUS, interval=INTERVALLUM,
                             progress=False, auto_adjust=False)
            if df is not None and not df.empty:
                break
        except Exception as e:
            utolso_hiba = e
        time.sleep(5)
    else:
        raise RuntimeError(f"nincs adat ({utolso_hiba})" if utolso_hiba else "nincs adat")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["High", "Low", "Close"]].dropna()
    if df.empty:
        raise RuntimeError("nincs adat")
    utolso = df.index[-1]
    if utolso.tzinfo is None:
        utolso = utolso.tz_localize("UTC")
    if utolso + pd.Timedelta(minutes=PERC) > pd.Timestamp.now(tz="UTC"):
        df = df.iloc[:-1]  # még be nem zárt gyertya
    return df


def elemez(df, p):
    c = df["Close"]
    gyors = c.rolling(p["gyors"]).mean()
    lassu = c.rolling(p["lassu"]).mean()
    d = c.diff()
    nyer = d.clip(lower=0).ewm(alpha=1 / p["rsi"], adjust=False, min_periods=p["rsi"]).mean()
    veszt = (-d.clip(upper=0)).ewm(alpha=1 / p["rsi"], adjust=False, min_periods=p["rsi"]).mean()
    rsi = 100 - 100 / (1 + nyer / veszt)
    elozo = c.shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - elozo).abs(),
                    (df["Low"] - elozo).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / p["atr"], adjust=False, min_periods=p["atr"]).mean()

    n = len(df)
    kul = gyors - lassu
    fel = ((kul.shift() <= 0) & (kul > 0)).to_numpy()
    le = ((kul.shift() >= 0) & (kul < 0)).to_numpy()
    ki, irany = None, 0
    for i in range(n - 1, 0, -1):
        if fel[i] or le[i]:
            ki, irany = i, (1 if fel[i] else -1)
            break
    r, a = float(rsi.iloc[-1]), float(atr.iloc[-1])
    jel, kor = 0, None
    if ki is not None:
        kor = n - 1 - ki
        if kor <= p["friss"] and not math.isnan(r) and not math.isnan(a):
            if irany > 0 and r < 70:
                jel = 1
            elif irany < 0 and r > 30:
                jel = -1
    return {"ar": float(c.iloc[-1]), "rsi": r, "atr": a, "jel": jel, "kor": kor,
            "kereszt_ido": str(df.index[ki]) if ki is not None else None,
            "utolso_ido": df.index[-1]}


def ar_szoveg(v):
    return f"{v:.2f}"


def szoveg_keszit(a, nev):
    tav = a["atr"] * P["atr_szorzo"]
    stop = a["ar"] - a["jel"] * tav
    cel = a["ar"] + a["jel"] * tav * P["rr"]
    irany = "LONG" if a["jel"] > 0 else "SHORT"
    s = f"{irany} {nev}: belépő {ar_szoveg(a['ar'])}, stop {ar_szoveg(stop)}, cél {ar_szoveg(cel)}"
    egyenleg = env_szam("EGYENLEG", None)
    fx = env_szam("FX", None)
    if egyenleg and fx:
        keret = egyenleg * env_szam("KOCKAZAT", 1.0) / 100
        egyseg = (tav + env_szam("SPREAD", 1.0)) * fx
        meret = round(math.floor(keret / egyseg / LEPES + 1e-9) * LEPES, 2)
        s += (f", méret {meret}, kockázat {meret * egyseg:.2f}, "
              f"margin {meret * a['ar'] * fx / LEV:.2f}")
    else:
        s += " (méret nincs: add meg az EGYENLEG és FX beállítást)"
    return s + ". Belépés előtt nézd meg az árat az appban."


def ertesit(tema, szoveg, cim="CFD jelzes"):
    req = urllib.request.Request("https://ntfy.sh/" + tema, data=szoveg.encode("utf-8"),
                                 headers={"Title": cim, "Priority": "high"})
    urllib.request.urlopen(req, timeout=15).read()


def allapot_olvas():
    try:
        with open(ALLAPOT_FAJL, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def allapot_ir(d):
    with open(ALLAPOT_FAJL, "w", encoding="utf-8") as f:
        json.dump(d, f)


def main():
    tema = os.environ.get("NTFY_TEMA", "").strip()
    if not tema:
        print("Hiányzik a NTFY_TEMA beállítás.")
        return 1
    if "--teszt" in sys.argv:
        ertesit(tema, "Teszt: az értesítés működik. Jelzésnél itt kapod a belépőt, stopot és célt.", "CFD teszt")
        print("Tesztértesítés elküldve.")
        return 0

    nev = "US Tech 100"
    ticker = os.environ.get("TICKER", "").strip() or "^NDX"
    try:
        a = elemez(letolt(ticker), P)
    except Exception as e:
        print("Adatlekérési hiba:", e)
        return 1

    kor_perc = (pd.Timestamp.now(tz="UTC") - (a["utolso_ido"] if a["utolso_ido"].tzinfo
                else a["utolso_ido"].tz_localize("UTC"))).total_seconds() / 60
    if kor_perc > 60:
        print(f"Az utolsó gyertya {kor_perc:.0f} perces, a piac valószínűleg zárva. Nincs teendő.")
        return 0

    print(f"Ár {a['ar']:.2f}, RSI {a['rsi']:.1f}, jel: {a['jel']}")
    if a["jel"] == 0:
        return 0
    allapot = allapot_olvas()
    kulcs = f"{a['kereszt_ido']}|{a['jel']}"
    if allapot.get("utolso") == kulcs:
        print("Erről a jelzésről már szóltam.")
        return 0
    ertesit(tema, szoveg_keszit(a, nev))
    allapot_ir({"utolso": kulcs})
    print("Értesítés elküldve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
          
