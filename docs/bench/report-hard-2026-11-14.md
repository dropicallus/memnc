# bench-report: swarm vs single (roadmap 14.15.9)

Runs: 24 | prompt version: 2026-10-31.1

| arm | tier | runs | success | rate | 95% CI | mean rounds | mean tok (in/out) |
|---|---|---|---|---|---|---|---|
| A (single-shot) | hard | 8 | 6 | 75% | [41%, 93%] | 0.00 | 610 |
| A (single-shot) | all | 8 | 6 | 75% | [41%, 93%] | 0.00 | 610 |
| B (self-reflect) | hard | 8 | 8 | 100% | [68%, 100%] | 0.75 | 2144 |
| B (self-reflect) | all | 8 | 8 | 100% | [68%, 100%] | 0.75 | 2144 |
| C (mesh) | hard | 8 | 6 | 75% | [41%, 93%] | 1.50 | 4369 |
| C (mesh) | all | 8 | 6 | 75% | [41%, 93%] | 1.50 | 4369 |

## errors (1)

- hard_dijkstra__C0: ReadTimeout: 

## failing checks (sample)

- hard_rate_limiter__A0: burst of 2 expected [True, True, False], got [False, False, False]
- hard_dijkstra__A0: checker error: TypeError: '<' not supported between instances of 'list' and 'int'
- hard_rate_limiter__C0: burst of 2 expected [True, True, False], got [False, False, False]
