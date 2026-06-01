# Illuminati — Trading Principles

> Hard-won rules, each tied to a real loss. Re-read this after any bad day
> BEFORE touching the system. The danger is never the losing day — it's
> overriding the rules because of it.

These 5 principles came out of **2026-06-01** (9 closes, 3W/6L, net **−$954**).
Nothing malfunctioned that day — the model picked real setups, every stop
fired on time, risk limits held. The losses came from *how trades were
entered*, not the engine. Each rule below now lives in code so you don't have
to enforce it by willpower.

---

## 1. A stop loss can't cover a gap

**What happened:** HOOD and WOLF were both bought Friday 2:09 PM with ~3%
stops, held over the weekend, and gapped down Monday — filling at **−7.2%**
and **−7.4%**, more than double the stop. The market was *flat* that day
(SPY −0.08%). This was pure weekend headline risk.

**The principle:** stops only work while the market trades continuously. Across
a gap (overnight, weekend, earnings) the stop becomes a market order that fills
at the next available price — which can be far below the trigger. The real
control isn't a tighter stop; it's **not opening exposure you can't trade
through.**

**Encoded as:** `BLOCK_FRIDAY_PM_ENTRIES` — no new entries after 2 PM ET
Fridays (`config.py`, enforced in `risk/portfolio_guard.py`). Would have
prevented ~$1,093 of the −$954 day (flipping it to roughly breakeven).

---

## 2. Never average up into a loser

**What happened:** ON was bought **5 times in a week** ($114 → $122 → $124 →
$123 → $120) as it declined. Each buy had a fresh signal, but the net effect
was betting more on being wrong. The duplicate guard relied on a stale
in-memory position list and missed it.

**The principle:** adding to a losing position is the most common way accounts
blow up. "Getting a better average" is a story you tell yourself while
increasing your bet on a thesis that isn't working. **One entry per thesis.**
If it's not working you don't get a do-over at a worse price — you wait for a
genuinely new setup.

**Encoded as:** `MAX_ENTRIES_PER_TICKER = 2` over a 5-day window, counted from
Alpaca buy fills directly (not the in-memory list that missed ON).

---

## 3. Confidence is about the setup, not the outcome

**What happened:** HOOD scored **89 — the highest of the day — and lost the
most.** That's not the model failing. The score grades the setup at entry
(volume, trend, structure). It cannot see the future.

**The principle:** a good decision can have a bad outcome; a bad decision can
get lucky. Judge trading by whether the rules were followed, **not** by whether
each trade won. Abandoning a good rule because of one bad outcome is how you
churn an account to death.

---

## 4. Win rate is a distribution, not a promise

**What happened:** the system runs ~60% win rate. June 1 was 3W/6L — a 33% day.
That's not broken; it's **variance.** In any 60% system, losing streaks of
3–5 in a row are mathematically guaranteed to occur regularly.

**The principle:** never evaluate a strategy on one day — evaluate over 50–100
trades. The danger isn't the losing day, it's *reacting* to it: overriding the
system, sizing up to "make it back," or quitting. The −$954 stayed inside the
−5% daily halt. The system did its job.

**Supported by:** the weekly review (Sunday Slack report) — win rate, avg win
vs avg loss, profit factor, rule violations. Look at the distribution, not the
day.

---

## 5. Cut losers fast, let winners run — that's the entire edge

**What happened:** today's asymmetry — losses of −7%, −7%, −3%, but **FPS won
+9.9%** because the trailing stop let it run, and WYFI +2.9%, FIG +2.2%.

**The principle:** profitability isn't "win more often" — it's **lose small,
win big.** The only two levers are (a) keeping losses smaller than wins and
(b) staying in winners long enough. AEGIS trailing stops do exactly this. Today
every loser was small-to-medium and stopped on time — the engine worked; the
leak was bad *entries*, not bad exits.

---

## The traps that will actually hurt you (all psychological)

- **Revenge trading** — "I lost $954, let me force a big trade to win it back."
  This is how a −$954 day becomes a −$5,000 day.
- **Overriding the guards** — disabling the Friday block or entry cap because
  you "have a feeling." The guards exist precisely for the moments you feel
  like overriding them.
- **Moving stops** — widening a stop because you don't want to take the loss.
  The loss is already real; the stop only decides *how big* it gets.

## The bottom line

The system encodes the discipline so you don't have to white-knuckle it.
"Not making bad trades" = **trusting the rules and resisting the urge to
override them on emotional days.** You're in paper validation — this is exactly
what it's for: surfacing these patterns with fake money before going live.

_Last updated 2026-06-01._
