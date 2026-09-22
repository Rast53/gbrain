# decision-stand report

Generated 2026-09-22T08:30:40Z; dataset LocalLLaMA/typed-decisions (fingerprint db479b4904cd05ee).

| model | n | accuracy | soft_acc | brier | tv | ece | score_mae | within_1_level | latency_ms_mean | latency_ms_p50 | cost_usd_total |
|---|---|---|---|---|---|---|---|---|---|---|---|
| results_jev.jsonl | 2000 | 0.7385 | 0.6853 | 0.2131 | 0.2939 | 0.1034 | 0.389 | 0.9925 | 497.2 | 488.0 | 0.0159 |
| results_laya_english.jsonl | 1540 | 0.3766 | 0.3372 | 0.4621 | 0.475 | 0.2563 | 0.6908 | 0.8945 | 3576.0 | 3999.0 |  |
| results_laya_multilingual.jsonl | 2000 | 0.352 | 0.3369 | 0.5174 | 0.4994 | 0.3522 | 0.7604 | 0.8538 | 1338.7 | 1378.0 |  |

## results_jev.jsonl

| workflow | n | accuracy | soft_acc | brier | ece |
|---|---|---|---|---|---|
| agent_trace_observability | 500 | 0.636 | 0.5597 | 0.2835 | 0.1814 |
| customer_service | 500 | 0.792 | 0.7416 | 0.1757 | 0.081 |
| invoice_processing | 500 | 0.784 | 0.7312 | 0.1357 | 0.0668 |
| security_incidents | 500 | 0.742 | 0.7085 | 0.2573 | 0.1405 |

| question type | n | accuracy | soft_acc | brier | ece | score_mae |
|---|---|---|---|---|---|---|
| choice | 600 | 0.7333 | 0.6366 | 0.1241 | 0.0382 |  |
| noul | 600 | 0.7933 | 0.7933 | 0.2879 | 0.2067 |  |
| score | 800 | 0.7013 | 0.6408 | 0.2237 | 0.1031 | 0.389 |

## results_laya_english.jsonl

| workflow | n | accuracy | soft_acc | brier | ece |
|---|---|---|---|---|---|
| agent_trace_observability | 500 | 0.388 | 0.3599 | 0.3218 | 0.2775 |
| customer_service | 500 | 0.382 | 0.3608 | 0.3709 | 0.186 |
| invoice_processing | 500 | 0.36 | 0.2865 | 0.7012 | 0.3597 |
| security_incidents | 40 | 0.375 | 0.3906 | 0.3672 | 0.2977 |

| question type | n | accuracy | soft_acc | brier | ece | score_mae |
|---|---|---|---|---|---|---|
| choice | 508 | 0.3327 | 0.2774 | 0.3319 | 0.1361 |  |
| noul | 416 | 0.4688 | 0.4688 | 0.8569 | 0.5312 |  |
| score | 616 | 0.3506 | 0.2976 | 0.3029 | 0.2002 | 0.6908 |

## results_laya_multilingual.jsonl

| workflow | n | accuracy | soft_acc | brier | ece |
|---|---|---|---|---|---|
| agent_trace_observability | 500 | 0.286 | 0.2808 | 0.505 | 0.4013 |
| customer_service | 500 | 0.42 | 0.4024 | 0.4552 | 0.3164 |
| invoice_processing | 500 | 0.308 | 0.2742 | 0.7675 | 0.4113 |
| security_incidents | 500 | 0.394 | 0.3902 | 0.3418 | 0.2879 |

| question type | n | accuracy | soft_acc | brier | ece | score_mae |
|---|---|---|---|---|---|---|
| choice | 600 | 0.295 | 0.2792 | 0.4994 | 0.3623 |  |
| noul | 600 | 0.4967 | 0.4967 | 0.7538 | 0.5033 |  |
| score | 800 | 0.2863 | 0.2604 | 0.3536 | 0.2313 | 0.7604 |
