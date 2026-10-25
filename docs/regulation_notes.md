# EU261 regulation notes

These notes explain, in plain English, the passenger rights the entitlement engine encodes, along with the
article or case each rule comes from. The machine-readable versions are
`config/rulesets/eu261_2004.yaml` and `config/rulesets/eu261_amended_2027.yaml`; the hand-worked examples are
in `tests/golden/cases.yaml`.

**Not legal advice.** These notes describe published rules for a software project.

## 1. Two regimes

| | `eu261_2004` | `eu261_amended_2027` |
|---|---|---|
| Legal text | Regulation (EC) No 261/2004, as interpreted by the CJEU | Same Regulation as amended by **Regulation (EU) 2026/2202** (OJ L, 2.10.2026) |
| Applies | flights before 23 Oct 2027 | from **23 Oct 2027** (entered into force 22 Oct 2026, Art. 3 of 2026/2202) |
| Selection in the engine | scheduled departure date < boundary | scheduled departure date ≥ boundary |

Regulation 2026/2202 has **no transitional rule**: it doesn't say whether a flight booked before 23 Oct 2027
but departing after it is governed by the old or the new rules. The engine picks the regime by scheduled
departure date, and marks every booked-before/departs-after case `unverified`, which routes it to a human
(`applies_to.booked_before_boundary`).

## 2. Who is covered (scope)

Under both regimes, a passenger is covered when:
1. the flight **departs from an airport in the EU**, whatever the airline; or
2. the flight departs from a non-EU airport, arrives in the EU, and the **operating** airline is an EU carrier,
   unless the passenger already received equivalent benefits, compensation *and* assistance in that country.
   (Whether they did is a factual question, so the engine sends it to review.)

The airline that counts is the one *operating* the flight, not the one whose flight number is on the ticket.
Free tickets and non-public reduced fares (e.g. staff tickets) are excluded; frequent-flyer tickets are covered.
The UK has its own post-Brexit version (UK261), which this project does not cover: a London → Paris flight on
British Airways is out of scope, but the same route on Air France is in scope (EU carrier arriving in the EU).

Norway, Iceland, Liechtenstein and Switzerland apply EU261 through separate agreements. Neither text mentions
them, so the engine marks them `unverified` until those agreements are checked.

Example: New York → Paris, 6 h late. On Delta: out of scope. On Air France: in scope, €600.

## 3. How much compensation

The amounts are the same in both regimes:

| Journey distance | Amount |
|---|---|
| ≤ 1,500 km | €250 |
| > 1,500 km within the EU, or 1,500–3,500 km otherwise | €400 |
| everything else | €600 |

**Distance** is the great-circle ("as the crow flies") distance from the **first departure airport to the final
destination**, ignoring stopovers: CJEU *Bossen* (C-559/16) for the 2004 regime, Art. 7(5) in the amended
text. Rome → Brussels → Hamburg is 1,326 km direct (€250), not the 1,655 km flown.

The engine logs the computed distance. Routes within 10 km of a band edge are flagged, because airport
coordinates and Earth-radius choices can flip them. Exactly 3,500 km is ambiguous ("between 1500 and 3500")
and is also flagged.

### Delays
| | 2004 regime | Amended regime |
|---|---|---|
| Compensation trigger | arrival **3 h or more** late at the final destination (*Sturgeon* C-402/07, *Nelson* C-581/10) | arrival **more than 3 h** late (Art. 6(4)) |
| "Arrival" means | a door opens (*Germanwings* C-452/13) | on-block time (Art. 2(x)) |
| Measured against | scheduled arrival | the ticket's arrival time at booking, or after a schedule change made ≥ 14 days before departure (Art. 2(o)) |

The boundary flips: **exactly 3h00 late pays under 2004 but not under the amended rules** (golden cases
G2004-03 and G2027-02).

### Optional 50 % reduction
Airlines *may* pay half when the passenger arrives not too late:
- **2004 (Art. 7(2)):** after rerouting, arrival within 2 h / 3 h / 4 h (by band). For long-haul delays of 3–4 h,
  the CJEU allowed €300 instead of €600; the paragraph that says so was read only in secondary sources, so it
  is `unverified`.
- **Amended (Art. 7(2)):** only the €600 band, arrival within 4 h.

The engine reports both the full amount and the "may reduce to" amount, because the reduction is the airline's
choice.

**New in the amended regime (Art. 7(3)):** if a cancellation is rerouted onto a flight that leaves more than an
hour *earlier* and the passenger takes it, compensation drops by 50 % (1–2 h earlier) or 25 % (2–3 h earlier).
Whether that reduction is automatic or optional depends on English wording that still needs confirming
(`unverified`).

## 4. Cancellations

| Notice given | 2004 regime: no compensation if… | Amended regime: no compensation if… |
|---|---|---|
| ≥ 14 days | always | always |
| 7–13 days | rerouted to leave ≤ 2 h early **and** arrive < 4 h late | rerouted to leave ≤ 1 h early **and** arrive < 2 h late |
| < 7 days | rerouted to leave ≤ 1 h early **and** arrive < 2 h late | same as above |

The airline must prove when it told the passenger, so an unknown notice date means compensation is owed. A
departure **brought forward by more than 1 h** counts as a cancellation (amended Art. 2(l)(ii); in the 2004
regime from *Azurair* C-146/20, marked `unverified`).

Example: Frankfurt → Lisbon (1,874 km), cancelled 10 days ahead and rebooked 1 h earlier, arriving 3 h late.
That's €0 under 2004 (inside the 2 h / 4 h window) but €400 under the amended rules (outside < 2 h).

The amended text adds a **diversion rule** (Art. 5(6)): landing at another airport serving the same city is not
a cancellation if the airline transfers the passenger to the booked airport and the arrival there is no more
than 3 h late.

## 5. Denied boarding and missed connections

**Denied boarding** (Art. 4): if a passenger is refused boarding against their will (e.g. overbooking), the
compensation is owed immediately, with no extraordinary-circumstances defence. Refusals for health, safety,
security or bad travel documents are not "denied boarding" (Art. 2(j)). Under the amended rules, refreshments
are due immediately and payment within 7 days; volunteers keep their compensation unless they agree otherwise
on a durable medium.

**Missed connections** on one booking count as a delay at the final destination (*Folkerts* C-11/11; amended
Art. 6b). Care is owed at the transfer airport. On separate tickets only the disrupted flight counts; under the
amended rules, sellers must warn passengers about this before purchase (Art. 14(3)). Which airline pays when
the delayed flight and the missed one are operated by different airlines is worded differently in the English
draft and the published Spanish text (`unverified`).

## 6. Extraordinary circumstances

The airline doesn't pay compensation if it **proves** the disruption was caused by an event outside its control
that couldn't have been avoided with all reasonable measures. This is a defence the airline must prove, not a
fact the engine assumes.

| Cause | 2004 regime | Amended regime |
|---|---|---|
| Bad weather making the flight unsafe | extraordinary | extraordinary (Annex 1(c)) |
| Air-traffic-control restrictions, airport closure | extraordinary | extraordinary (Annex 3(d)–(e)) |
| Bird strike | extraordinary (*Pešková* C-315/15) | extraordinary (Annex 3(i)) |
| Ordinary technical fault | **not** extraordinary (*Wallentin-Hermann* C-549/07, *van der Lans* C-257/14) | not extraordinary; only hidden manufacturing defects revealed by the manufacturer or an authority (Annex 3(c)) |
| The airline's own staff on strike | **not** extraordinary (*Airhelp* C-28/20; wildcat: *Krüsemann* C-195/17) | not extraordinary, unless the demands can only be met by public authorities (Annex 3(h)) |
| Strike by airport or air-traffic-control staff | extraordinary | extraordinary (Annex 3(g)) |
| Event that hit the aircraft's previous flight | possible with a direct causal link (*LE v TAP* C-74/19) | not addressed |

Cases the engine can't decide from the inputs (previous-rotation knock-on effects, unknown causes) are
classed `review` and handed off.

**Extraordinary circumstances never remove care or refund/rerouting rights** (*McDonagh* C-12/11). The amended
text only allows capping the hotel at 3 nights.

## 7. Right to care (meals, calls, hotel)

| | 2004 regime | Amended regime |
|---|---|---|
| When care starts for delays | departure delay ≥ 2 h (≤ 1,500 km), ≥ 3 h (1,500–3,500 km or intra-EU > 1,500 km), ≥ 4 h (longer); distance of the delayed flight | from the scheduled departure, no distance thresholds (Art. 6(2), recital 31) |
| Food | meals and refreshments "in reasonable relation to the waiting time" | refreshments every 2 h; a meal after 3 h, then every 5 h, max 3 a day |
| Communication | two calls, e-mails, etc. | internet access and two calls |
| Hotel | when a night's stay is needed, plus transport; no cap | same; capped at **3 nights** in extraordinary circumstances, but **not** for passengers with special needs (Art. 9(6), 11(10)); no euro cap |
| Cancellations / denied boarding | always | always; refreshments immediately on denied boarding |
| Special needs (reduced mobility, unaccompanied children, …) | care "as soon as possible", no thresholds (Art. 11(2)) | same (Art. 11(9)); also pregnant passengers and passengers needing medical assistance |

A departure delay of **5 h or more** gives the right to a refund instead of travelling (2004: Art. 6(1)(iii);
amended: Art. 6(3)). Under the amended rules, care ends if the passenger takes a refund at the origin or
chooses a later-date rerouting (Art. 9(7)). Self-arranged care is reimbursed within 14 days if "necessary,
reasonable and proportionate".

## 8. Refunds, rerouting and vouchers

- **Choice:** refund, rerouting at the earliest opportunity, or rerouting at a later date of the passenger's
  choosing (Art. 8).
- **Refunds:** within **7 days** in both regimes; automatic by bank transfer under the amended rules.
- **Amended additions:**
  - rerouting may use other airlines or other modes of transport;
  - if no rerouting is offered within 3 h, the passenger may arrange their own and be reimbursed up to 400 %
    of the ticket price within 14 days.
- **Vouchers** never replace cash without the passenger's agreement:
  - 2004: "signed agreement" (Art. 7(3));
  - amended: explicit consent on a durable medium, worth at least the amount owed, valid for at most 12 months,
    and refunded automatically if unused (Art. 8a).
- The support agent must state the cash right whenever it mentions a voucher.

## 9. Claim deadlines

| 2004 regime | Amended regime |
|---|---|
| None in the Regulation; national limitation periods apply (*Moré* C-139/11), e.g. Germany 3 years, Netherlands 2 years (examples, `unverified`) | **9 months** from the actual departure date on the ticket. The airline must send rights information within 96 h of the journey's end and pay or explain a refusal within 30 days (Art. 7(4), 7(9)) |

## 10. Encoding decisions

1. **Boundaries are explicit per rule.** "3 h or more" (gte) vs "more than 3 h" (gt); "does not exceed"
   (lte); "less than" (lt). Each rule in the YAML names its comparison.
2. **Minutes, timezone-aware.** Delays are whole minutes computed from timezone-aware datetimes; notice is
   measured against the scheduled departure.
3. **Two distances.** The compensation band uses origin → final destination. The 2004 care thresholds use the
   delayed flight's own distance.
4. **Defences are not facts.** An unknown cause or a previous-rotation cause → `review` (handoff), never
   "extraordinary" by default.
5. **Optional reductions are reported, not applied.** Output shows `compensation_eur` and `may_reduce_to_eur`.
6. **Unverified rules hand off.** Any rule listed under `unverified:` that a case touches sets
   `confidence: unverified`, and the agent hands off instead of stating an amount (FR-4.7). Boundary rules
   (3,500 km, exactly 7 days' notice) only count as touched when a case falls on the boundary.
7. **Cancellation care follows the waiting time.** Meals and refreshments are owed "in reasonable relation
   to the waiting time", so the engine grants cancellation care when the passenger learns of the cancellation
   on the day of travel, or when the notice date is unknown. With earlier notice there is no airport wait. A
   hotel is granted when the case says a night's stay is needed (`nights_needed`).
8. **Country codes are quoted in YAML.** An unquoted `NO` (Norway) parses as boolean `false`; the territory
   loader rejects anything that isn't a two-letter string.
9. **Citations for the amended regime use the published OJ article numbers.** Several paragraphs were
   renumbered between the June 2026 joint text and the final OJ (e.g. the notice rule is 5(5), formerly
   5(6)).

## 11. Open points (`unverified`)

| Key | Why |
|---|---|
| `applies_to.booked_before_boundary` (amended) | No transitional rule in 2026/2202 |
| `scope.territory.eea_and_switzerland` (both) | EEA and EU–Switzerland incorporation decisions not checked |
| `compensation.boundary_3500_km` (both) | Exactly 3,500 km: €400 or €600 |
| `compensation.reduction_on_rerouting.delay_long_haul` (2004) | *Sturgeon* para. 63 read only in secondary sources |
| `compensation.earlier_departure_reduction` (amended) | Optional or automatic: English OJ wording |
| `cancellation.brought_forward_minutes_counts_as_cancellation` (2004) | *Azurair* C-146/20 not read in the primary text |
| `cancellation.notice_exactly_7_days` (2004) | Overlapping tier wording at exactly 7 days |
| `missed_connection.responsible_carrier` (amended) | "Disrupted" vs "delayed" flight wording |
| `claim_deadline.national_limitation_years` (2004) | Examples, not checked per country |
| `extraordinary_circumstances.cause_codes.*` (several) | Guidelines/recital readings, or not addressed in the text |

The refund clock is worded differently in the two texts: 7 days "from the passenger's request" (joint text)
vs "from the date of the passenger's choice" (OJ). The obligation and the amount don't change, so it isn't
flagged.

## 12. Sources

- Regulation (EC) No 261/2004, OJ L 46, 17.2.2004: https://eur-lex.europa.eu/eli/reg/2004/261/oj
  (text as adopted: https://www.legislation.gov.uk/eur/2004/261/adopted)
- Commission Interpretative Guidelines, OJ C 214, 15.6.2016
- Regulation (EU) 2026/2202, OJ L, 2.10.2026: http://data.europa.eu/eli/reg/2026/2202/oj
  (Spanish OJ text: https://www.boe.es/doue/2026/2202/L00001-00045.pdf)
- Joint text PE-CONS 39/26 (English, before legal-linguistic revision):
  https://data.consilium.europa.eu/doc/document/PE-39-2026-INIT/en/pdf
- CJEU: Sturgeon C-402/07 & C-432/07; Nelson C-581/10; Germanwings C-452/13; Folkerts C-11/11; Bossen C-559/16;
  Wallentin-Hermann C-549/07; van der Lans C-257/14; Pešková C-315/15; Krüsemann C-195/17; Airhelp C-28/20;
  LE v TAP C-74/19; McDonagh C-12/11; Moré C-139/11 (press releases on https://curia.europa.eu)
- Cross-check: Burges Salmon, "Evolution delivered: what Regulation (EU) 2026/2202 means for EU261 reform"
