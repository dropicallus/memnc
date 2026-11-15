# bench-report: swarm vs single (roadmap 14.15.9)

Runs: 24 | prompt version: 2026-10-31.1

| arm | tier | runs | success | rate | 95% CI | mean rounds | mean tok (in/out) |
|---|---|---|---|---|---|---|---|
| A (single-shot) | medium | 8 | 8 | 100% | [68%, 100%] | 0.00 | 521 |
| A (single-shot) | all | 8 | 8 | 100% | [68%, 100%] | 0.00 | 521 |
| B (self-reflect) | medium | 8 | 8 | 100% | [68%, 100%] | 0.75 | 1913 |
| B (self-reflect) | all | 8 | 8 | 100% | [68%, 100%] | 0.75 | 1913 |
| C (mesh) | medium | 8 | 8 | 100% | [68%, 100%] | 1.88 | 4346 |
| C (mesh) | all | 8 | 8 | 100% | [68%, 100%] | 1.88 | 4346 |
