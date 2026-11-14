# Design Note: The Urgency Formula

## Goal

Answer one question quickly: **"What should I work on first?"** The score
should be simple enough to explain in one line, cheap to compute, and work with
no network or machine learning.

## Formula

```
urgency = priority x (estimated hours / days left)
```

Where `days left = deadline - today`, floored at 0.5, and the score is doubled
if the task is overdue (a boost, not a guarantee of first place).

## Why this shape

1. **Hours / days left is a "required daily effort" rate.** A 6-hour task due
   in 2 days needs 3 hours per day; a 6-hour task due in 12 days needs only
   half an hour per day. A task that demands more of each remaining day is
   riskier to postpone. A pure "earliest deadline first" rule ignores this and
   would put a 15-minute task above a 10-hour project due one day later.
2. **Priority is a multiplier** so the student keeps control. Exam prep and a
   club newsletter can have the same rate but should not rank equally.
3. **Linear and explainable.** Doubling the priority doubles the score;
   halving the days left doubles the score. A student can predict how editing
   a task will change the ranking.

## Edge cases

| Case | Handling |
|---|---|
| Due today (0 days) | Use 0.5 days, so the task scores very high but never divides by zero |
| Overdue | Same 0.5-day floor, then x2, so late work ranks above an identical task due today (a very small overdue task can still rank below a large one due soon; the overdue warning covers it) |
| Ties | Earlier deadline wins, then lower id (stable and predictable) |
| Completed tasks | Excluded from ranking, warnings, and overload checks |

## Overload check

Pending hours are grouped by deadline date. If one date has more than 8 hours
due, the app warns. This is a heuristic, not a schedule: it flags days where
work is likely to pile up so the student can start earlier.

## Known limitations

- It assumes work can be done on any day; it does not know about classes,
  weekends, or free time.
- Hours are estimates; a bad estimate gives a bad score.
- The rate for a far-away large task stays low until the deadline nears, which
  can cause a late crunch. The overload warning exists to catch this.
- Overdue tasks double their score, but the multiplier is a design choice
  rather than a derived value.

## Possible improvements

- Split big tasks across days to spread load
- Skip weekends or user-defined busy slots
- Use progress (hours already spent) instead of the full estimate
- Learn the student's real vs estimated hours over time
