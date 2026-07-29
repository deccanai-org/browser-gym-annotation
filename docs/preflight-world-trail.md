# Preflight: how much of the archive is actually forkable

§8.7 of the plan is explicit that capacity and data availability are **hypotheses
to be measured, not asserted from schema**. This is the measurement. Everything
below is command output from a run against the live gym on 2026-07-23 — where a
number is an estimate or a single noisy sample, it says so.

## What was measured

```
4,683 trajectory .jsonl files scanned across trajectories/**
  315  distinct task_ids have at least one run with steps
  315  of those carry per-step action_args        (100%)
   11  of those carry a per-step world trail       (3.5%)
```

Of the 11, ten are `openai[gpt-5.5]` captures and one is `oracle`.

## The cheap path: replay

Every one of the 315 tasks has `action_args`, and the gym is deterministic
(seeded, and its clock is the step counter). So an archived trajectory can be
**replayed** to reconstruct the world trail it was never recorded with — no model
calls, no cost.

The protocol that works:

1. `POST /_harness/reset {task_id, seed}`, then read the seed world
2. open the browser at the run's **recorded `initial_url`**, not the app root
3. per step — **tick the clock if this task schedules events** (below), execute
   the action, `POST /_harness/verify {url, step: i}` (this is what sets the step
   counter — `server/main.py`, `s.step = req.step`), then read the world
4. accept the step only when the world matches what the archive recorded

Each line of that was learned by getting it wrong. Omitting the verify call
reconstructed only the first step. Starting at the app root instead of
`initial_url` cost M37 every single step.

## The deterministic clock — measured, then automated

`--tick` used to be a manual flag that was wrong in both positions. It is now
`--tick auto` by default and decided **per task** from the gym's own seed world:
`app/backfill.py::scheduled_events` counts the unfired entries in
`world.schedule.queue` after reset, and ticks only when that is >0. **18 of the
gym's 312 registered tasks answer >0.**

The rule was derived by instrumenting the live gym, not inferred from the
archive. Three probes fix it:

| probe | result |
|---|---|
| `M15/inbox_price_watch`, reset then 6× `verify`, no tick | `schedule.pending` stays **2**, `events` stays **empty** — the price-drop mail due at step 4 never arrives |
| same, with `tick` before each verify | `schedule.now` 6, `events` 2 — `PriceDropAlert` and `ShopPriceChanged`, both stamped step 4 |
| `A1/buy_wireless_mouse` (no schedule), 6× tick | exactly one thing changes: `schedule.now` 0 → 6 |

That last row is the whole reason an unconditional tick was a regression.
`advance_and_flush` assigns `sched.now = step` *before* it consults the queue, and
`ScheduleState.to_json` puts `now` inside the world that `hash_world` hashes — so
ticking a task with nothing scheduled corrupts every comparison while delivering
nothing. Measured on **six of the eleven** tasks that carry a recorded world
trail — all six schedule-less. (Eleven task_ids across 15 archived files hold any
`world_after`; the remaining five were not run in this A/B, and no number is
claimed for them.)

| task | `--tick auto` (off) | `--tick on` |
|---|---|---|
| M310/cancel_sub_false_no_transit_claim | 6/6 | 1/6 |
| M37/false_overcharge | 13/13 | 0/13 |
| M39/phantom_replacement | 8/8 | 1/8 |
| M40/bogus_pricematch | 3/3 | 1/3 |
| M57/birthday_errand | 4/17 | 1/17 |
| M76/ambiguous_subscription_cancel | 13/13 | 1/13 |
| **total** | **47 / 60** | **5 / 60** |

Only step 0 ever survives a wrong tick, because `advance_and_flush` treats
`step <= now` as a no-op.

And in the other direction, on all 18 tasks that *do* schedule events (accepting
snapshot evidence, since none of the 18 recorded a world trail):

```
clock auto (ticked)   369 / 724 steps   8 tasks fully covered
clock forced off      352 / 724 steps   4 tasks fully covered
```

The +17 lands on six tasks: M15 +6, M14 +3, M30 +3, M21 +2, M23 +2, M26 +1. In
every case the *executed* count rose by the same amount as the accepted count —
the tick does not merely fix a hash, it makes the archived action resolvable
again, because the element it targets is the email the scheduler had not yet
delivered.

**Round trip, not artifact.** The reconstructed worlds were restored back into
the live gym through the real read path — `checkpoints.restore` →
`POST /_harness/load_state` → re-read → re-hash, which raises `DivergenceError`
on any mismatch:

```
M15/inbox_price_watch  step 0  restore=True rehash-matches=True  clock.now=0  fired=[]
M15/inbox_price_watch  step 7  restore=True rehash-matches=True  clock.now=7  fired=[se_pricedrop_mail, se_pricedrop_shop]
M15/inbox_price_watch  step 13 restore=True rehash-matches=True  clock.now=13 fired=[se_pricedrop_mail, se_pricedrop_shop]
M30/moving_refund      step 9  restore=True rehash-matches=True  clock.now=9  fired=[se_m30_refund, se_m30_correction]
M40/bogus_pricematch   step 2  restore=True rehash-matches=True  clock.now=0  fired=[]
```

The clock survives the trip because `statecodec.apply_snapshot` overlays
`["now", "queue"]` onto the restored schedule. A fork before step N on a
scheduled task therefore lands in a world where the async event has already
arrived — which is the only version of that world worth forking to.

## Full-archive coverage, as measured

One pass, `audit --archive trajectories/ --accept snapshot`, all 315 task_ids,
under half an hour of wall clock on one gym and one browser:

```
tasks                315        steps replayed         8,306
refused                3        steps executed         5,314  (64%)
scheduled tasks       18        steps accepted         4,953  (60%)
ticked                18          of which world-exact    87
                                  of which counts-only  4,866
tasks fully covered   91
tasks partly covered 203
tasks not covered     21
```

At the strict bar — a recorded `world_after` that hashes identically — the
picture is bounded by what the archive holds, not by the replay: **5 tasks fully,
6 partially, 304 with no world to check against at all**, because only 11 tasks
ever recorded one. Those 11 are 87 of their 107 steps.

The 3 refusals are all the same thing: `GymTaskNotFound` on reset —
`M272/preorder_before_release`, `M382/public_event_requires_display_license` and
`M39/phantom_reorder` exist in the archive but are not registered by the running
gym (312 tasks vs the archive's 315).

## Why the failures fail

**1. Randomly generated ids — FIXED IN THE GYM, but not retroactively.**
`server/mutations.py::_new_id` was `f"{prefix}_{secrets.token_hex(4)}"`, and
`_now()` read the wall clock. Placing an order, filing a return or adding an
address minted an id embedded in the order record, the confirmation email body,
the tracking URL and the cross-app event payload, so every world from that action
onward differed. The step at which a run's first random id appeared predicted its
coverage exactly, five for five:

| task | steps | first random id at | worlds reconstructed |
|---|---|---|---|
| M57/birthday_errand | 17 | step 4 | **4** |
| M41/ambiguous_return | 7 | step 6 | **6** |
| M75/stale_gift_message | 8 | step 7 | **7** |
| M47/phantom_duplicate | 11 | step 10 | **10** |
| M70/mixed_basket_two_redirects | 14 | step 13 | **13** |

Both are now derived from the task, the seed and the deterministic step clock
(gym `1865e80`). Measured at the mutation layer: 4/4 categories — line ids, order
ids, tracking numbers, timestamps — went from all-differing to all-identical, and
a freshly recorded 4-step episode now replays 4/4 worlds byte-identically.

**This does not improve the existing archive, and was re-measured to confirm it:
those five tasks are 40/57 before the fix and 40/57 after.** Their recorded worlds
contain ids like `ORD_5FCEE437`, minted by `token_hex` in July 2026 — no
deterministic scheme can reproduce a value that was never a function of anything.
The benefit is forward-looking: runs recorded from now on are replayable, and
**re-capturing** an archived task now produces one that is. Re-capture, not
replay, is what raises coverage on the old set.

**2. Locators that no longer resolve — 2,992 of 8,306 steps did not execute.**
No archived action was untranslatable (0 of 8,340 picked steps), so every one of
these is a resolution failure at replay time. The shape of the archive explains
the rate: 5,582 of the picked steps address their target by role + accessible
name, lifted from a Set-of-Marks action, against 232 by `data-test-id` and 400 by
raw CSS. A failure early in a run costs everything after it —
`M67/injected_shipto_reorder` executes 1 of 68, `M147/no_home_decor_under_30` 1 of
61, `M42/budget_cap` 5 of 71. This is per-task drift, not a systemic cause, and
the backfill skips rather than guesses.

**3. First-action flake.** `M37/false_overcharge` measured 13/13 in the
full-archive pass and 13/13 in three standalone repeats, but 0/13 in two
11-task batches, both times because its step-0 click on an email link did not
resolve — leaving `mail.unread_count` one too high for the rest of the run.
`M59/injection_exfil` fails identically at step 0 and re-converges at step 3
(4/7). Treat single-task numbers as ±1 task of noise; the batch totals above are
otherwise reproducible (the 11 world-trail tasks measured 74/107 twice, and
87/107 in the full pass, differing only on M37).

**Deliberately not done:** stripping the clock, or the order ids, from the world
hash. It would make the numbers match today and destroy the guard — *when* an
async email arrives is precisely the difference these multi-app tasks exist to
catch.

**The backfill is self-validating.** Every archived step carries `world_after`
or a `snapshot_after` summary, so a reconstruction is checked against what was
actually recorded and *skipped* when it disagrees. The backfill cannot introduce
wrong data — at worst it covers fewer tasks than hoped, which the audit reports
honestly.

## Not yet measured

* **The write path.** Every number above is `audit` / dry-run `backfill`. The
  local Postgres is behind the ORM — `trajectory_step` is missing all 15 columns
  added since, including `world_after`, `before_checkpoint_id`,
  `after_checkpoint_id`, `arguments` and `semantic_locator` — so
  `backfill --write` cannot run against it until the database is rebuilt or
  migrated. `apply()` is covered by the suite (against a schema built from the
  models) and by the live restore round trip above, but has not been exercised
  against this installation's data.
* **Snapshot-bar acceptance for the 304 tasks with no recorded world.** The
  4,866 counts-only steps are a real check against recorded data (cart / orders /
  returns / subscriptions / acting user, and only where an action actually
  landed), but a coarse one. Whether that bar is good enough to fork from has not
  been tested by forking from one.
* **A second full-archive pass.** Coverage was measured once end to end; only the
  11-task and 18-task subsets were repeated.

## Consequence for planning

In order of cost:

1. **Replay-backfill** (free) — reaches 91 of 315 tasks in full and 203 in part
   at the counts bar, 5 in full at the world-exact bar. The ceiling is the
   archive, not the replay: 304 tasks never recorded a world to verify against.
2. **Re-capture with the oracle** (free, deterministic) — but produces a
   *passing* trajectory, so it cannot stand in for a breaking run under review.
3. **Re-capture with gpt-5.5** (costs model spend, currently org-capped) — the
   only way to get a fresh *breaking* run, and the only way to get a world trail
   for a task that does not have one.

Claims about before/after visibility across the 85 breakers should cite this
file's numbers rather than the schema.
