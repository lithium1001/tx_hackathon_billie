| Metric | n | Result | 95% CI |
|---|---|---|---|
| Parse: whole draft correct (pass^3 = 90%) | 60 | 55/60 = 91.7% | [81.9%, 96.4%] |
| Parse: field-level accuracy | 312 | 272/312 = 87.2% | [83.0%, 90.4%] |
| Clarify: asked when it should | 30 | 30/30 = 100.0% | [88.6%, 100.0%] |
| Clarify: asked when it should not | 30 | 0/30 = 0.0% | [0.0%, 11.4%] |
| Injection: blocked by the shield | 60 | 30/60 = 50.0% | [37.7%, 62.3%] |
| Injection: swayed draft reached the confirm screen | 60 | 0/60 = 0.0% | [0.0%, 6.0%] |
| Postings without a valid signature | 180 | 0 | — |
| Gateway rejection tests | — | all pass | — |
| Audit completeness (streams) | 677 | 677/677 = 100.0% | [99.4%, 100.0%] |
| Gateway audit chain verifies | 180 | 180/180 = 100.0% | [97.9%, 100.0%] |
| System time to confirm screen (median) | 108 | 7.22 s | — |

Injection funnel (all trials):

| Stage | Count |
|---|---|
| shield | 30 |
| not_swayed | 24 |
| validator | 0 |
| risk | 6 |
| reached_confirm | 0 |
