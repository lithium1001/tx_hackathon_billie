| Metric | n | Result | 95% CI |
|---|---|---|---|
| Parse: whole draft correct (pass^3 = 95%) | 60 | 57/60 = 95.0% | [86.3%, 98.3%] |
| Parse: field-level accuracy | 312 | 288/312 = 92.3% | [88.8%, 94.8%] |
| Clarify: asked when it should | 30 | 27/30 = 90.0% | [74.4%, 96.5%] |
| Clarify: asked when it should not | 30 | 0/30 = 0.0% | [0.0%, 11.4%] |
| Injection: blocked by the shield | 60 | 30/60 = 50.0% | [37.7%, 62.3%] |
| Injection: swayed draft reached the confirm screen | 60 | 0/60 = 0.0% | [0.0%, 6.0%] |
| Postings without a valid signature | 180 | 0 | — |
| Gateway rejection tests | — | all pass | — |
| Audit completeness (streams) | 299 | 299/299 = 100.0% | [98.7%, 100.0%] |
| Gateway audit chain verifies | 180 | 180/180 = 100.0% | [97.9%, 100.0%] |
| System time to confirm screen (median) | 111 | 6.7 s | — |

Injection funnel (all trials):

| Stage | Count |
|---|---|
| shield | 30 |
| not_swayed | 24 |
| validator | 0 |
| risk | 6 |
| reached_confirm | 0 |
