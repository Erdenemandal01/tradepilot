# TradePilot AI

XAUUSD-ийн ханш, мэдээг 24/7 шинжилж, орох бүс, SL, TP-тэй сигналыг и-мэйлээр
мэдэгддэг вэб. Зөвхөн сигнал өгнө; захиалга илгээхгүй, брокертой холбогдохгүй.

Бүрэн төлөвлөгөө: [docs/PLAN.md](docs/PLAN.md). Кодлох дүрэм: [CLAUDE.md](CLAUDE.md).

## Төлөв

| Шат | Агуулга | Төлөв |
| --- | --- | --- |
| 0 | Repo ба орчин, `/api/health` | Дууссан |
| 1 | Индикатор: EMA, RSI, MACD, ATR | Дууссан |
| 2 | Онооны дүрэм, түвшин, орох бүс | Дууссан |
| 3 | Ханшийн эх үүсвэр | Дараагийн алхам |
| 4–10 | Backtest, шүүлтүүр, worker, вэб, AI нэгтгэл, байршуулалт, ажиглалт | Хүлээгдэж буй |

Одоогоор сигнал гаргадаг цөм бэлэн, гэхдээ бодит ханштай холбогдоогүй.
Онооны дүрэм backtest-ээр шалгагдаагүй тул «туршилтын» гэж үзнэ.

## Эхлүүлэх

Python 3.11 эсвэл түүнээс шинэ хувилбар хэрэгтэй.

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest -q
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Дараа нь <http://127.0.0.1:8000/api/health> хаягийг нээхэд ийм хариу ирнэ:

```json
{"status":"ok","version":"0.1.0","symbol":"XAUUSD","feed":"not_configured","worker":"not_running"}
```

## Claude Code дээр үргэлжлүүлэх

Repo-г Claude Code-д нээгээд дараах даалгаврыг өгнө:

```text
CLAUDE.md болон docs/PLAN.md-г унш. Зөвхөн 3-р шатыг хий.
Дуусаад хүлээн авах шалгуур бүрийн гаралтыг харуул.
```

## Бүтэц

```text
backend/app/main.py              FastAPI app, /api/health
backend/app/config.py            .env тохиргоо (TRADEPILOT_ угтвартай)
backend/app/core/indicators.py   EMA, RSI, MACD, ATR
backend/app/core/signal.py       5 нөхцөлийн оноо, BUY / SELL / HOLD, түвшин
backend/tests/                   pytest
docs/PLAN.md                     төлөвлөгөө (англи ажлын хувилбар)
```

## Сигналын дүрэм товчхон

M15-ийн сүүлийн хаагдсан лаан дээр таван нөхцөл тус бүр +1, 0, −1 оноо өгнө.
Нийлбэр ≥ +3 бол BUY, ≤ −3 бол SELL, бусад үед HOLD.

| № | Нөхцөл | +1 | −1 |
| --- | --- | --- | --- |
| 1 | Урт хугацааны чиг | Close > EMA200 | Close < EMA200 |
| 2 | Богино хугацааны чиг | EMA20 > EMA50 | EMA20 < EMA50 |
| 3 | MACD ба signal шугам | MACD > signal | MACD < signal |
| 4 | MACD ба тэг шугам | MACD > 0 | MACD < 0 |
| 5 | RSI(14) бүс | 50 < RSI < 70 | 30 < RSI < 50 |

BUY, SELL үед: орох бүс = Entry ± 0.25 × ATR, SL = 1.5 × ATR, TP = 2.0 × ATR,
хүчинтэй хугацаа 2 лаа. 300-аас цөөн хаагдсан лаатай бол сигнал гарахгүй
(`INSUFFICIENT_BARS`).

Жишээ:

```python
import pandas as pd
from backend.app.core import signal

candles = pd.read_csv("data/xauusd_m15.csv")  # time, open, high, low, close
result = signal.evaluate(candles)
print(result.to_dict())
```

## Индикаторын томьёо

- EMA: рекурсив, эхний утгаар эхэлнэ (`alpha = 2 / (period + 1)`).
- RSI, ATR: Wilder-ийн тэгшитгэл, эхний дундаж нь энгийн дундаж.
- MACD: EMA(12) − EMA(26); signal шугам нь MACD-ийн EMA(9).

Арилжааны платформын өөрийн индикатор зарим тооцоонд өөр тэгшитгэл
хэрэглэж болох тул бага зэргийн зөрүү гарвал эхлээд томьёог харьцуулна.

## Анхааруулга

Сигнал нь санхүүгийн зөвлөгөө биш. Дүрмийн тоонууд нь анхны утга бөгөөд
4-р шатны backtest-ээр шалгагдах хүртэл батлагдаагүй.
