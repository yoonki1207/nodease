# Model-routing convergence verification

This package tests base revision `9ad27d5abceb527fa32e6612ffe6b6917450cef5`. It does not change production routing thresholds or algorithms.

The fixed synthetic corpus has 300 training cases and 150 holdout cases (100/50 low, medium, and high cases). Holdout scenario families use different problem constructions. Three order seeds reuse the same training cases and holdout; they are an order-sensitivity check, not three independent datasets. Gold requirements are authored expectations, not independently adjudicated ground truth. The corpus hash is `041d72a7afc90ffa9ee28a50671ea63826db0fdbcac28211da1bb421550c7257`.

Run from the repository root with the workflow-engine Python environment and `PYTHONPATH` set to that root:

```sh
python -m scripts.model_routing_verification
python -m scripts.model_routing_verification --execute --mode offline-gold --output-dir /tmp/routing-offline-new
python -m scripts.model_routing_verification --execute --mode live-judge --max-cost-usd 30 --output-dir /tmp/routing-live-new
```

The existing experiment command also accepts `--verify-convergence` followed by these options. It dispatches before the old runner's reset path. Existing legacy behavior is otherwise preserved.

`offline-gold` uses the real pinned E5 encoder and production learner with authored training labels. `live-judge` uses the same encoder/learner with actual provider-generated requirements. Neither mode executes deployed workflows, publishes a learner in PostgreSQL, or evaluates final answer quality; its overall verdict is always `INCOMPLETE`, alongside a separate classifier verdict.

Full persisted verification is available through `--mode persisted --user-id UUID --organization-id UUID`. It requires an already prepared isolated database with those subjects and usable provider credentials, plus `NODEASE_ROUTING_VERIFICATION_ISOLATED_DB=1`. It creates fresh application/workflow/deployment/learner records per seed. It records 300 real automatic workflows, drains learning, and requires an actually published local-first version before evaluating 150 frozen holdout cases against middle/high model baselines. If publication fails, it records `learner_not_ready` and skips answer-quality calls. It does not force local mode or reset existing learners. Follow repository policy: PostgreSQL integration executes in remote CI, not a local database.

The dedicated CI workflow executes the PostgreSQL publication/reload/runtime bridge and offline verification contracts. Its bridge test substitutes predictions and operational success rates to isolate database wiring. It does not demonstrate real-encoder generalization, real answer quality, or a fully live persisted workflow.

Live calls require explicit `--execute`. Every provider attempt reserves a conservative text-token cost before network I/O. Missing usage/timeouts block all later calls in that process. USD 30 is the maximum accepted per invocation; multiple invocations must share the remaining task budget manually. The reported amounts are token-based estimates, not invoices. Standard GPT-5.4 Mini prices were checked at https://developers.openai.com/api/docs/models/gpt-5.4-mini (USD 0.75 input / 4.50 output per million tokens). The completed standalone run uses the default official endpoint. No tools, regional processing, or priority tier is requested.

Reports exclude prompts, responses, credentials, and model weights. Persisted mode uses normal workflow logging inside the isolated database, so synthetic inputs/outputs exist there; do not run it on a production database or use sensitive data. No existing application data is deleted by the verifier. Output files cannot be overwritten or resumed implicitly.

A complete PASS requires corpus-bound IDs/labels, immutable nonempty published-state evidence, actual local routing coverage, requirement accuracy, zero observed high-impact underestimation, no collapsed prediction axes, complete quality scores, and paired quality noninferiority against both baselines. A complete failed evaluation is `FAIL`; missing required evidence is `INCOMPLETE`. Passing harness tests does not mean the routing model passed the experiment.

Protection-boundary review: saved resources use fresh experiment UUIDs; management API/UI changes are not applicable; preflight requires isolated-DB opt-in and explicit execution subjects; runtime uses existing credential resolution behind the shared budget facade; existing credential lifecycle rules are unchanged; reports redact payloads, while the isolated DB retains normal synthetic workflow logs. The DB bridge is remotely tested; the full live persisted path remains unexecuted.

The persisted evaluator also checks the selected model when predicted requirements exactly match gold. It computes allowed choices from the fixed catalog and graph before training, using the production capability/price selector with gold inputs. This isolates classifier-to-selector consistency, not the independent correctness of the selector itself. It permits a single model when every case legitimately allows it; it does not impose arbitrary model quotas. Incorrect requirement predictions remain subject to the original axis/exact-match thresholds rather than an added 100% accuracy requirement.
