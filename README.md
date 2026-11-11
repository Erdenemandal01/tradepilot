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
| 3 | Ханшийн эх үүсвэр | Код бэлэн; 24 цагийн цуглуулга, MT5-тай 20 харьцуулалт хүлээгдэж байна |
| 4–10 | Backtest, шүүлтүүр, worker, вэб, AI нэгтгэл, байршуулалт, ажиглалт | Хүлээгдэж буй |

Сигнал гаргадаг цөм бэлэн, Binance XAUUSDT-ээс бодит ханш татаж чаддаг.
Онооны дүрэм backtest-ээр шалгагдаагүй тул «туршилтын» гэж үзнэ.

## Эхлүүлэх

Python 3.11 эсвэл түүнээс шинэ хувилбар хэрэгтэй.

Windows PowerShell (`python` ажиллахгүй бол `py` гэж бичнэ):

```powershell
py -m venv .venv
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

## 3-р шат: ханшийн эх үүсвэрийг шалгах

Анхдагч эх үүсвэр нь Binance XAUUSDT (түлхүүр шаардахгүй). Twelve Data-г
хэрэглэх бол `.env`-д `TRADEPILOT_TWELVEDATA_API_KEY`-г бичээд, доорх команд
бүрд `--source twelvedata` гэж нэмнэ. Тэр үед цуглуулгын файл нь
`data/collect_twelvedata_M15.csv` болно.

1. **24 цаг тасралтгүй цуглуулах.** Компьютер унтахгүй, интернет тасрахгүй байх ёстой:

   ```powershell
   .venv\Scripts\python -m backend.scripts.collect_candles --hours 24
   ```

   Дуусаад (эсвэл явцад нь) шалгана:

   ```powershell
   .venv\Scripts\python -m backend.scripts.check_candles data/collect_binance_M15.csv
   ```

   Шалгалт хоёр зүйлийг хардаг: сүүлийн тасралтгүй хэсэг 24 цаг хүрсэн эсэх,
   хадгалсан лаа бүр эх үүсвэрийн одоогийн утгатай таарч байгаа эсэх. 24 цаг
   дүүрээгүй үед үр дүн `NOT YET` гэж гарна, энэ нь алдаа биш.

2. **MOT Capital MT5-тай 20 удаа харьцуулах.** Өөр өөр цагт хийнэ:

   ```powershell
   .venv\Scripts\python -m backend.scripts.compare_prices
   ```

   MT5 дээр XAUUSD-ийн bid/ask-ийг харах агшинд Enter дарж, харсан
   тоонуудаа 30 секундэд багтаан `4062.08 4062.21` гэж бичнэ. Удсан эсвэл
   энэ хооронд ханш их хөдөлсөн бол хэмжилт хадгалагдахгүй, дахин эхэлнэ.
   Enter-ийн оронд `u` гэвэл сүүлийн хэмжилтийг устгана, `q` гэвэл зогсоно.
   Амралтын өдөр алтны зах хаалттай тул хэмжилт хийхгүй.

   Дүгнэлт гарахад 20 хэмжилт, тэдгээр нь дор хаяж 6 өөр цагт хийгдсэн байх
   шаардлагатай:

   ```powershell
   .venv\Scripts\python -m backend.scripts.compare_prices --report
   ```

Үр дүн `data/`-д хадгалагдана (git-д орохгүй).

## Claude Code дээр үргэлжлүүлэх

Repo-г Claude Code-д нээгээд дараах даалгаврыг өгнө:

```text
CLAUDE.md болон docs/PLAN.md-г унш. 3-р шатын шалгуурын үр дүнг хар,
эх үүсвэрийг сонгоход тусал. 4-р шатыг асуухгүйгээр бүү эхлүүл.
```

## Бүтэц

```text
backend/app/main.py              FastAPI app, /api/health
backend/app/config.py            .env тохиргоо (TRADEPILOT_ угтвартай)
backend/app/core/indicators.py   EMA, RSI, MACD, ATR
backend/app/core/signal.py       5 нөхцөлийн оноо, BUY / SELL / HOLD, түвшин
backend/app/core/candles.py      лааны шалгалт, тасалдал, M15 -> H1
backend/app/core/pricecheck.py   эх үүсвэр ба брокерийн ханшийн зөрүү
backend/app/datasource/          Binance, Twelve Data, CSV эх үүсвэр
backend/scripts/                 цуглуулах, тасалдал шалгах, ханш харьцуулах
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
