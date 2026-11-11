# Dashboard mock-up review, 2026-11-11

Five reviewers were started on the mock-up of the web dashboard (plan section 11); two finished before the
review was cut short: the Mongolian copy and usability. The format, layout / accessibility and content
lenses did not finish. Nothing below has been applied to the mock-up yet. Line numbers refer to the
mock-up's desktop artboard as first published.

## Mongolian copy

The Mongolian is competent and mostly natural: clean Cyrillic throughout (I checked for Latin/Cyrillic homoglyphs and stray characters, none; Ө/Ү are the right code points), consistent ханш (live quote) vs үнэ (level) usage, and trader vocabulary that largely matches how people here talk. «Орох бүс», «Алдагдал зогсоох», «Ашиг авах», «Хүчинтэй хугацаа», «Зэрэглэл», «Үндэслэл», «Оноо», «Сигналын түүх», «Хожлын хувь», «Дундаж үр дүн (R)», «Ашгийн фактор», «Эдийн засгийн календарь», «AI мэдээний нэгтгэл», «Системийн мэдэгдэл», «ХҮИ (CPI)» and «лаа» can stay as they are. The one line I would not ship is the sample alert «Ханш сэргэлээ. Сигнал үргэлжилж байна.», which in market Mongolian says the gold price rebounded; beyond it there are three suffix slips («SL хүрсэн», «100-аас», «түвшинг»), four wordings a trader would misread or feel as translated («Тэнцэх хожлын хувь», «Төвийг сахисан», «сүүлийн 7», the Хүчинтэй / Нээлттэй / Хугацаа дууссан trio) and one price named two ways («Жишиг үнэ» / «Орох үнэ»). The three «Орох бол» steps are conditional, agree with PLAN 5.3 and do not read as an instruction to trade (only two small clarity edits); the sticky note and board titles are clean apart from one suggested safety line.

### 1. Alert «Ханш сэргэлээ» reads as "gold rebounded", not "price feed restored"

- Severity: high
- Where: Main.dc.html L475 (system alerts card; the same text would go out by e-mail)

In Mongolian market language «ханш сэргэх» is the standard phrase for a price rebound («алтны ханш сэргэлээ»). «Ханш сэргэлээ. Сигнал үргэлжилж байна.» therefore reads as market commentary: "the price has recovered, the signal still stands". Alerts are also e-mailed (L486), where the outage line below is not there to disambiguate. «Сигнал үргэлжилж байна» is ambiguous too: one signal still running, or signal generation resumed.

**Fix.** Replace L475 with «Ханш ирж эхэллээ. Сигнал дахин ажиллаж байна.» It mirrors the outage line L482 («Ханш 5 минут ирээгүй. Сигнал түр зогслоо.»), which can stay. Outside this artboard, States.dc.html has the same verb: «Ханш сэргэх хүртэл сигнал гарахгүй.» -> «Ханш дахин ирэх хүртэл сигнал гарахгүй.»

### 2. «SL хүрсэн» / «TP хүрсэн» are missing the dative case

- Severity: medium
- Where: Main.dc.html L333, L342 (log pills), L349, L350 (legend pills)

«хүрэх» (to reach) governs the dative: the price reaches the SL («SL-д хүрэх»; traders write «TP-дээ хүрлээ»). Without the suffix «SL хүрсэн» parses as "the SL arrived", a telegraphic translation of "SL hit". The artboard suffixes Latin abbreviations everywhere else («TP-г», «FOMC-ийн», «Backtest-ийн»), so the bare form is also inconsistent.

**Fix.** L333 «SL хүрсэн · 09:45» -> «SL-д хүрсэн · 09:45»; L342 «SL хүрсэн · 08:00» -> «SL-д хүрсэн · 08:00»; L349 «TP хүрсэн» -> «TP-д хүрсэн»; L350 «SL хүрсэн» -> «SL-д хүрсэн».

### 3. Gate criteria sentence: wrong suffix «100-аас», loose "at least", calqued «зардлын дараа»

- Severity: medium
- Where: Main.dc.html L457, first sentence

(a) 100 is read «зуу», which ends in a long vowel, so the ablative is «зуугаас» and the written form is «100-гаас», not «100-аас». (b) PLAN section 6 says "at least 100 trades"; «-аас дээш» is "above", the unambiguous form is «доошгүй». (c) «сүүлийн 30% өгөгдөлд» has no genitive link, and «зардлын дараа эерэг дундаж үр дүн» is word-for-word "positive average result after costs".

**Fix.** Replace the first sentence of L457 with: «Шалгуур: өгөгдлийн сүүлийн 30%-д 100-гаас доошгүй арилжаа, зардал хассаны дараа дундаж үр дүн эерэг.» Keep the second sentence («Backtest 4-р шатанд, бодит бүртгэл 6-р шатнаас эхэлнэ.») as it is.

### 4. «Тэнцэх хожлын хувь» reads as "the win rate needed to pass"

- Severity: medium
- Where: Main.dc.html L456

The everyday meaning of «тэнцэх» is "to qualify / to pass" («шалгалтад тэнцэх», «тэнцсэн»). Sitting under the chip «Backtest-ийн шалгуур даваагүй» and right above «Шалгуур: ...», «Тэнцэх хожлын хувь 42.9%» will be read as "win rate required to pass the gate: 42.9%". That is not the gate (100 trades and positive expectancy after costs), so the line misinforms. Break-even needs its object stated.

**Fix.** L456 -> «Ашиг, алдагдал тэнцэх хожлын хувь 42.9% (SL 1.5 × ATR, TP 2.0 × ATR, спред тооцоогүй).»

### 5. Log statuses «Хүчинтэй», «Нээлттэй», «Хугацаа дууссан» contradict the card's own use of "хугацаа"

- Severity: medium
- Where: Main.dc.html L288, L297/L306/L315/L324, legend L348-L352

The signal card defines "time" as the 30-minute entry window («Хүчинтэй хугацаа», step 3 «хугацаа дууссан бол хөөж орохгүй»). The 14:00-15:30 rows are past that window yet show «Нээлттэй», while the legend offers a separate «Хугацаа дууссан»: by the card's wording those four rows are exactly «хугацаа дууссан». A trader can also read «Нээлттэй» as "still open to enter" or as an open MT5 position («нээлттэй позиц»), which this service never has. «Хүчинтэй» appears in the table but not in the legend, and neither it nor «Нээлттэй» is an «үр дүн». PLAN section 6 does not define when a tracked signal "expires", so the fourth pill has no rule behind it.

**Fix.** Keep the pills «Хүчинтэй» and «Нээлттэй», define them in the legend and rename the colliding one. L348 «Үр дүнгийн төрөл:» -> «Тайлбар:». Legend content: [Хүчинтэй] «орох хугацаа дуусаагүй» · [Нээлттэй] «орох хугацаа дууссан, TP, SL-ийн алинд нь ч хүрээгүй» · [TP-д хүрсэн] · [SL-д хүрсэн]. L352 «Хугацаа дууссан» -> «Үр дүнгүй дууссан», or drop the pill until the tracker has a rule for closing a signal without TP or SL. (Adds about one line of height to the log card.)

### 6. One price, two names: «Жишиг үнэ» on the signal card, «Орох үнэ» in the log

- Severity: medium
- Where: Main.dc.html L94 and L274 (first log row shows the same 4066.34)

4066.34 is «Жишиг үнэ» in the card and sits under the column «Орох үнэ» in the log. A third, different price is «орсон үнэ», the user's own fill (L109, L117, L135). «Орох үнэ» and «орсон үнэ» are one letter apart, so the log header blurs the distinction the card is built on: levels come from the reference price, distances are applied to your own fill. «Жишиг үнэ» itself is acceptable and matches the plan's "reference"; a trader would expect "Entry / Орох үнэ", but here that word must stay free for the user's fill.

**Fix.** L274 header «Орох үнэ» -> «Жишиг үнэ». Leave L94 as it is and keep «орсон үнэ» only for the user's own entry.

### 7. «сүүлийн 7» reads as "last 7 days"

- Severity: medium
- Where: Main.dc.html L265

«сүүлийн 7» is the stock opening of «сүүлийн 7 хоног» and it sits in the header of a history table, so the natural reading is "last 7 days". Intended: the last 7 of today's 21 signals.

**Fix.** «Өнөөдөр 21 сигнал · сүүлийн 7 · мэдээний шүүлтүүргүй» -> «Өнөөдрийн 21 сигналаас сүүлийн 7 нь · мэдээний шүүлтүүргүй»

### 8. «Төвийг сахисан» is diplomatic neutrality, the machine-translation default for "neutral"

- Severity: medium
- Where: Main.dc.html L393

«төвийг сахих» is what a state or a mediator does. As the value of «Алтны чиг хандлага:» it reads as a literal translation of "neutral"; the participle has no sensible agent (a trend cannot "keep neutrality"). The natural Mongolian word for a neutral bias is «саармаг».

**Fix.** «Төвийг сахисан» -> «Саармаг». For the other two digest values use «Өсөх» (bullish) and «Унах» (bearish) so the three read as one set after «Алтны чиг хандлага:».

### 9. «B зэрэглэл» is never explained, and Latin B is indistinguishable from Cyrillic В

- Severity: medium
- Where: Main.dc.html L86 (chip), L273 and the B cells of the log, L468; the gloss belongs in the AI card just above L401

Nothing on the page says what the grade measures or what the scale is. In a Cyrillic interface the Latin "B" (verified U+0042) renders exactly like Cyrillic «В», the third letter («вэ»), and Mongolian lists and school classes are lettered А, Б, В. Read that way «В зэрэглэл» is the third and lowest tier, not the middle of A-B-C. «Зэрэглэл» itself is the right word.

**Fix.** Add one 12px caption line in the AI card above L401: «Зэрэглэл: A = мэдээ сигналтай нийцсэн · B = мэдээ саармаг эсвэл нэгтгэл байхгүй · C = мэдээ сигналын эсрэг». The A-B-C sequence also settles the script. Chip and column text stay unchanged.

### 10. «түвшинг» should be «түвшнийг»

- Severity: low
- Where: Main.dc.html L401

«түвшин» ends in a dental н with a fleeting vowel (genitive «түвшний», like «өвчин -> өвчний, өвчнийг»), so the accusative is «түвшнийг». «түвшинг» follows the pattern of velar-н nouns («байшинг») and is a widespread but non-normative spelling.

**Fix.** «Сигналын чиглэл, түвшинг өөрчлөхгүй.» -> «Сигналын чиглэл, түвшнийг өөрчлөхгүй.»

### 11. Bare genitive «Туршилтын» on the signal card can be taken for "demo"

- Severity: low
- Where: Main.dc.html L87 (chip); L163 (KPI value) for reference

«Туршилтын» is an attributive form with no noun to attach to. Alone next to «B зэрэглэл» it can be read as «туршилтын данс», which is how brokers here say demo account, not as "experimental: backtest gate not passed".

**Fix.** L87 «Туршилтын» -> «Туршилтын сигнал». L163 can stay: its label «Дүрмийн төлөв» supplies the noun, and a longer value (for example «Туршилтын шатанд») would wrap at 22px in the tile.

### 12. «Орох бол» steps: sound, two small clarity edits

- Severity: low
- Where: Main.dc.html L134-L135

Verdict asked for: the steps are conditional («Орох бол»), written in the procedural -на / -гүй register rather than as imperatives, agree with PLAN 5.3 and do not read as an instruction to trade; «хөөж орохгүй» is exactly the trader idiom. Two rough spots: in step 1 «Ханш орох бүс дотор байгааг» garden-paths as «ханш орох» + «бүс» ("the zone the price enters") and «байгааг шалгана» presupposes the answer; step 2 gives two signed numbers and leaves the reader to map them to SL and TP by order.

**Fix.** L134 -> «Ханш бүс дотор байгаа эсэхийг MT5 дээрээ шалгана.» L135 -> «SL, TP-г өөрийн орсон үнээс зайгаар нь тавина: SL −8.32, TP +11.09.» L136 unchanged.

### 13. News-window sentence stacks three case endings

- Severity: low
- Where: Main.dc.html L383

«мэдээнээс 30 минутын өмнөөс 15 минутын дараа хүртэл» mirrors English "from 30 minutes before to 15 minutes after": grammatical, but hard to parse on first read and translated in feel. PLAN 5.2 also limits the window to high-impact USD events, which the sentence drops.

**Fix.** L383 -> «Өндөр нөлөөтэй USD мэдээний өмнөх 30, дараах 15 минутад сигнал гарахгүй.»

### 14. «мэдүүлэг» is the wrong word for jobless claims

- Severity: low
- Where: Main.dc.html L381 (sample calendar row)

«мэдүүлэг» is a declaration or return («татварын мэдүүлэг», «гаалийн мэдүүлэг»). Jobless claims are requests for benefit; Mongolian business news usually writes «ажилгүйдлийн тэтгэмж хүсэгчдийн тоо». Traders scan the calendar for the English name, which the CPI row already gives in brackets.

**Fix.** «Ажилгүйдлийн тэтгэмжийн мэдүүлэг» -> «Ажилгүйдлийн тэтгэмжийн хүсэлт (Jobless Claims)»

### 15. «Хамгийн их уналт (R)» can be read as gold's biggest fall; R is never defined

- Severity: low
- Where: Main.dc.html L444; L456 for the R note

«уналт» is first of all a price fall, and Mongolian traders say "drawdown" untranslated, so the label alone is ambiguous in a gold dashboard. "R" appears in two rows without a definition. The neighbouring labels («Хожлын хувь», «Дундаж үр дүн (R)», «Ашгийн фактор», «Хамгийн урт дараалсан алдагдал») are understandable as they are.

**Fix.** L444 «Хамгийн их уналт (R)» -> «Хамгийн их уналт (max drawdown), R». Append to the end of L456: « 1R = SL хүртэлх зай.»

### 16. Header chip says all data is «жишээ» while the sticky note says the signal is «бодит»

- Severity: low
- Where: Main.dc.html L29; canvas.json notes.data.text

The header chip labels everything «жишээ өгөгдөл», but the note says the candles, the 16:00 BUY and the log are «бодит» and reserves «Жишээ» for three cards. The same word then means "invented" on the cards and "not live" in the header. The note also never says that the BUY shown is a frozen 16:08 moment that has since expired, which matters for an owner who trades by hand.

**Fix.** L29 «Загвар · жишээ өгөгдөл» -> «Загвар · бодит цагийн өгөгдөл биш». In the note, add after the first paragraph: «Энэ бол 16:08-ын агшин: уг сигналын хугацаа аль хэдийн дууссан, арилжаанд ашиглахгүй.»

### 17. «Гарах» next to trade-entry wording also means "exit the trade"

- Severity: low
- Where: Main.dc.html L38 (log-out button)

On this page «орох» means entering a trade («Орох бүс», «Орох бол», «хөөж орохгүй»), so a lone «Гарах» button is its natural opposite, "exit". Position makes log-out the likely reading, but PLAN section 11 wants nothing that can be taken for a trade control.

**Fix.** «Гарах» -> «Системээс гарах»

### 18. H1 time axis prints dates with a slash while the header uses dots

- Severity: low
- Where: Main.dc.html L548 (script); visible on the H1 view as «11/10», «11/11»

Midnight ticks on the H1 chart render as «11/10» and «11/11», while the header date is «2026.11.11». Mongolian numeric dates are written with dots; two formats on one screen look unedited.

**Fix.** In L548 change the separator '/' to '.' so the labels read «11.10» and «11.11».

## Usability

On a laptop the mock-up passes the three-second test: the left card gives side, entry zone, SL / TP and time left in reading order, and the page matches the owner's reference (navy card grid, thin-border cards, pills, small uppercase cyan labels, rail, footer legend) with no gradients, emoji or accent-border tropes and with sample data honestly marked. Its weak point is the step from this page to MT5: the card's largest numbers are Binance prices, step 1 says to check them on MT5, and the only broker reading on record sits about 6 USD under the feed, twice the width of the entry zone. It also gives no cue that this BUY is the fifth in a row with four unresolved, and on a phone the rail sits between the header and the signal, so SL / TP land on the fold. Filler is limited to one reference price and a fourth KPI tile that repeats the card's chip; the roughly 34 pills are busy but ordered. Nothing was rendered: all positions are arithmetic from the styles, and the lint was run on a private copy.

### 1. The signal card's levels are Binance prices, but the card tells the trader to check them on MT5

- Severity: high
- Where: <scratch>/canvas/project/Main.dc.html — hero card: line 91 ('Орох бүс'), lines 104-109 and 112-117 (SL / TP boxes), line 134 ('Орох бол' step 1)

After BUY, the largest numbers on the page are the zone (26px) and SL / TP (22px). All three are Binance XAUUSDT prices, yet nothing inside the card says so: the source appears only as a KPI sub-label (line 149) and a grey footnote at the end of the chart legend (line 227). Step 1 then says 'Ханш орох бүс дотор байгааг MT5 дээрээ шалгана', i.e. compare the MT5 price with a Binance zone. The one MT5 reading on record (PLAN section 10: ask 4062.21 at 07:44 UTC) is 5.87 below the low of the artboard's own candle for that quarter-hour (M15 row 38, low 4068.08), more than twice the whole zone (2.78 wide). If that gap is typical (Phase 3's 20 readings will tell), MT5 shows about 4059 at the snapshot: a trader following step 1 sees 'below the zone' while the card shows the green check, and an SL typed as 4058.02 sits about 1 USD from price. For a SELL the same copy-over turns -8.32 / +11.09 into roughly -14 / +5. The card's own step 2, PLAN 5.3 and the Levels docstring in backend/app/core/signal.py all say the distances are what gets applied, but the distances are the 12px line. Side effect of the current label row: 'SL Алдагдал зогсоох' needs about 128px in a box about 126px wide at the 1440 frame, so it probably wraps while 'TP Ашиг авах' does not, putting the two numerals on different lines (estimated from the styles, not rendered).

**Fix.** (a) Line 134, replace the step with: 'Ханш бүс дотор эсэхийг эндээс харна. Түвшнүүд Binance-ийн ханшаар тул MT5-ийн ханштай тулгахгүй.' (b) Line 91: 'Орох бүс' -> 'Орох бүс · Binance ханшаар'. (c) In both boxes make the distance the figure and the Binance price the footnote: label row 'SL' + 'орсон үнээс' (and 'TP' + 'орсон үнээс') instead of 'Алдагдал зогсоох' / 'Ашиг авах'; 22px line '−8.32' / '+11.09' in the existing #E6EDF7; 12px line 'Binance 4058.02' / 'Binance 4077.43'. Step 2 stays as written. Cost: (a) and (b) are copy only (step 1 grows by one line) and are right whatever Phase 3 finds. (c) removes the large SL / TP prices from the card (they stay on the chart's axis tags and in the log), reverses the order of the e-mail template in PLAN 5.5, and leaves the trader to add and subtract on MT5, which step 2 already asks; revert it if Phase 3 ends with a feed that tracks MT5 to a few cents. Optional and new material, so ask the owner first: one row under the boxes with a labelled input 'MT5 дээр орсон үнэ' and two computed outputs (SL = price − 8.32, TP = price + 11.09 for a BUY), which removes that arithmetic; it needs an input handler from the runtime, which the condensed rules only document for onClick.

### 2. On a phone the icon rail sits between the header and the signal, so SL / TP land on the fold

- Severity: medium
- Where: <scratch>/canvas/project/Main.dc.html — line 42 (the flex row holding <nav> and <main>); also line 406 and line 94

At 390px the row on line 42 wraps and the rail becomes its own 58px strip above <main>. The header wraps to four rows there (brand; 'Загвар' pill; status pill; clock + 'Гарах'; about 187px), so roughly 290px precede the signal card. By arithmetic from the styles the SL / TP numerals land at about 645-670px and their distance lines at about 675-690px: on or under the fold of an iPhone-class viewport (about 660px visible). The real site, without the 'Загвар' pill, gains only about 35px. PLAN 11.2 asks that the signal and what to do with it be readable on a phone without scrolling. Five unlabeled jump links are the least useful thing to put above the signal. Lower down, the alerts card (did the feed drop, was the e-mail sent) comes after six rows of dashes.

**Fix.** Line 42: 'flex-wrap: wrap' -> 'flex-wrap: wrap-reverse'. On a laptop the row is a single line, so nothing changes; on a phone the rail drops below the footer and the card moves up 74px (numerals at about 570-595px). Line 406: the same change puts the alerts card above the empty statistics table on a phone. Optional: delete line 94 ('Жишиг үнэ 4066.34'), a sixth price the trader cannot use; the check line then fits on one row (about 23px less) and the validity bar also clears the fold. The KPI tiles still sit between the signal and the chart, against PLAN 11's 'signal card first, then the chart'; moving them under the chart would cost the reference's tiles-above-chart row, so leave that choice to the owner. Cost: on a phone the jump links are only at the bottom of a page about 4,200px long; the reference price is then shown only in the log.

### 3. Nothing marks this BUY as the fifth in a row, and the log's status words overlap

- Severity: medium
- Where: <scratch>/canvas/project/Main.dc.html — lines 85-88 (hero chips), 288, 297 / 306 / 315 / 324, 347-353 (log pills and legend)

The log shows BUY at 14:00, 14:30, 15:00, 15:30 and 16:00: a new one each time the previous 30-minute validity ends, four of them without an outcome. The hero card looks exactly as it would for a first signal, and on a phone the chart markers and the log that reveal the run are two to three screens down. A trader who enters on each e-mail holds five longs. In the log, 'Хүчинтэй' and 'Нээлттэй' sit side by side unexplained; 'Нээлттэй' reads as 'still open to enter' or as an open MT5 position, which the system cannot know. The legend omits 'Хүчинтэй' and lists 'Хугацаа дууссан', which no row uses and which cannot be told apart from 'Нээлттэй' (every 'Нээлттэй' row's entry period has already run out). PLAN section 6 names 'expired' as an outcome but gives no rule for it.

**Fix.** (a) After line 87 add a third chip in the neutral style of line 86: '5 дахь BUY', with title="14:00-оос хойш дараалсан 5 дахь BUY. Өмнөх 4-ийн үр дүн гараагүй." The three chips need about 290px of the 322px row, so they do not wrap. (b) Line 288: 'Хүчинтэй' -> 'Хүчинтэй · 16:30 хүртэл'. (c) Lines 297, 306, 315, 324 and 351: 'Нээлттэй' -> 'Үр дүн гараагүй'. (d) Lines 348-352: label 'Төлөв:', then each pill followed by a few plain words: 'Хүчинтэй' + 'орох хугацаа дуусаагүй'; 'Үр дүн гараагүй' + 'орох хугацаа дууссан, TP, SL-д хүрээгүй'; 'TP хүрсэн'; 'SL хүрсэн'. Delete the 'Хугацаа дууссан' pill until the owner defines that outcome, and add the question to PLAN section 10. Cost: one more chip in a card that already has two (it states a fact from the log, not advice); the legend grows from one row to about two on a laptop.

### 4. The states board leaves out 'price has left the zone', and its HOLD headline reads as position advice

- Severity: medium
- Where: <scratch>/canvas/project/States.dc.html (third board in canvas.json) — no card for a valid signal with the price outside the zone; card 1 line 28 ('HOLD') and line 21

Main shows only the go state. States adds no signal, news window and feed down, but not the state that step 3 of the card depends on: time left, price outside the zone, do not chase. The owner cannot judge how firmly the card says no in the one case where acting is wrong. Separately, card 1's 40px headline 'HOLD' is not among the terms PLAN 11.1 keeps in English, and to a trader it means 'keep your position'; someone still in the 16:00 BUY would read it as advice the service does not give.

**Fix.** Add a fourth card carrying the board's 'жишээ' chip: pill 'Бүсээс гарсан' (amber style of line 56); headline 'BUY' at 40px in #A9B6CC with the sub-line 'XAUUSD · M15 · 16:00-д хаагдсан лаа'; box with 'Ханш орох бүсээс гарсан. Хөөж орохгүй.' (14px, 600) and 'Ханш бүсэд буцаж орвол 16:30 хүртэл хүчинтэй.' (13px); footer 'Хугацаа дуусвал энэ сигнал хүчингүй болно.' No price is needed, so nothing is invented. Card 1: line 28 'HOLD' -> 'Сигнал алга' at 28px like the other two headlines, and delete the pill on line 21 that would repeat it. Cost: the fixed board needs a fourth column: root width 1240px -> 1645px, grid repeat(3, ...) -> repeat(4, ...), and the same width in $preview and in canvas.json.

### 5. On a phone the log's outcome column is off-screen, and 'Орох үнэ' names a different price than the card's 'орсон үнэ'

- Severity: medium
- Where: <scratch>/canvas/project/Main.dc.html — lines 271-277 (table header) and the seven rows, lines 281-343

The table keeps min-width: 600px inside a card about 332px wide at 390px. What shows without sideways scrolling is Цаг, Чиглэл, Зэрэглэл (B on every row until the digest exists) and Орох үнэ; SL, TP and the outcome, the point of a log, are off-screen with no cue that the table scrolls. Also the header 'Орох үнэ' (4066.34) is the price the hero calls 'Жишиг үнэ', while the hero's 'орсон үнээс' means the trader's own fill: two near-identical words for two different prices, which blurs the 'apply the distance to your own fill' message.

**Fix.** Reorder the columns, in the header and in every row, to: Цаг, Чиглэл, Үр дүн, Жишиг үнэ, SL, TP, Зэрэглэл; rename the header on line 274 from 'Орох үнэ' to 'Жишиг үнэ'. The first three columns need about 275px, so time, side and outcome are visible on a phone. Cost: markup reordering only (eight rows of cells); on a laptop the outcome pill moves from the right edge to the middle of the row.

### 6. 'Experimental' is said three ways on one page, once inaccurately, and takes a KPI slot

- Severity: low
- Where: <scratch>/canvas/project/Main.dc.html — lines 161-165 (fourth KPI tile), line 411 (statistics pill), line 457

The hero chip says 'Туршилтын'; the fourth KPI tile repeats it in 22px amber bold, the loudest text in that row, with 'Backtest хийгдээгүй'; the statistics pill says 'Backtest-ийн шалгуур даваагүй', which reads as run and failed although no backtest exists. Line 457 says '100-аас дээш арилжаа' (more than 100) where PLAN section 6 says at least 100. A KPI slot holding a word that will not change for weeks is the nearest thing to filler on the page.

**Fix.** Line 411: 'Backtest-ийн шалгуур даваагүй' -> 'Backtest хийгдээгүй'. Line 457: '100-аас дээш арилжаа' -> 'дор хаяж 100 арилжаа'. Tile 4: give the slot to the number the first finding depends on: label 'MT5-тай зөрүү', value '—' in the tiles' normal 24px mono #E6EDF7, sub-line '20 харьцуулалт хүлээгдэж байна'. Cost: that is a data item not in PLAN section 7, so it needs the owner's yes, and 'Backtest хийгдээгүй' then appears only in the statistics card (the 'Туршилтын' chip stays in the hero). If the owner declines, keep the tile and set its value to 18px #E6EDF7 so it stops competing with BUY.

