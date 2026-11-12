# On-demand jobs

These two are deliberately **not** installed as timers, and the repo ships no
launchd job for them.

`wiki-maintenance` and `dragonscale-tiling` used to run on a Sunday-03:00 timer on
an always-on machine. On a laptop that timer is close to useless — the machine is
asleep at 03:00, so the run lands whenever the lid next opens, which is both
unpredictable and the worst possible moment for two LLM-heavy jobs. (Since
2026-11-12 `wiki-maintenance` uses no model: autofix and lint, seconds.)

Run them by hand instead:

    ~/projects/vault-engine/maintenance/wiki-maintenance.sh
    ~/projects/vault-engine/maintenance/run-tiling-check.sh

The risk this trades into is that nothing runs for months and nobody notices:
nothing prompts a run by itself. A SessionStart staleness tripwire (in the caller's hooks) closes that loop — it
nags inside the Claude session when the last completion marker gets old.

A scheduled job is not a verified job: keep the tripwire even if a timer comes
back, because it also catches a job that runs on schedule and *fails*.
