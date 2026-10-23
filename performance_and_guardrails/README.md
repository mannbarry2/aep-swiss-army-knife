# performance_and_guardrails/

How the platform is performing, and whether it is inside Adobe's guardrails.

| Tool | What it does |
|---|---|
| [`platform_guardrails_scoreboard.py`](platform_guardrails_scoreboard.py) | Re-measures the guardrail scoreboard for a sandbox from the APIs. |
| [`platform_guardrails_scoreboard_slide.py`](platform_guardrails_scoreboard_slide.py) | Turns the scoreboard into a one-page slide. |
| [`profile_enabled_datasets_per_class_census.py`](profile_enabled_datasets_per_class_census.py) | Profile-enabled datasets per XDM class, against the per-class guardrail. |
| [`batch_audience_evaluation_timing.py`](batch_audience_evaluation_timing.py) | How long batch audience evaluation actually takes. |
| [`profile_snapshot_timing_history.py`](profile_snapshot_timing_history.py) | When the nightly segmentation and Profile snapshot ran, and how long they took. |
| [`audience_count_board_real_counts_at_8am.py`](audience_count_board_real_counts_at_8am.py) | The real profile count of every audience built yesterday, at 8am. |

Run any tool from the repo root: `python performance_and_guardrails/<tool>.py --help`.
