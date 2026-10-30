# Capacity

Capacity configuration and interpretation of existing evidence. This module does not generate traffic.

| File | Responsibility |
| --- | --- |
| `admission.py` | Candidate configuration and admission for calibration. |
| `performance.py` | Parse per-flow and per-shard capacity measurements and performance criteria. |
| `verdict.py` | Capacity verdicts for the API and legacy planner. |
| `receipts.py` | Check consistency among the manifest, native results, and cleanup receipts. |
| `promotion.py` | Validate an existing capacity grant; the optional startup path still uses it. |

This move adds no capacity test or five-cycle threshold. Public requests still allow one or mixed slices with a total of 1–100 UEs. The existing distinction between an explicit AI trial and verified capacity remains.
