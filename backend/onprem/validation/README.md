# Native scenario validation

These modules validate whether AI can retain policy during normal traffic and adjust QER under controlled constraints. The coordinator and Agent runtime still use them.

| File | Responsibility |
| --- | --- |
| `normal.py` | Arm, ownership claim, and completion receipts for a normal scenario. |
| `constrained.py` | Registration, claim, installation intent, quarantine, and status for a constrained scenario. |
| `controller.py` | Coordinate fixture installation and restoration through the native QER adapter. |
| `native.py` | Native snapshots and identity checks for original, installed, inner-restored, and fully restored states. |
| `restore.py` | Bind original values, save restore intent, verify restoration, and release a restored fixture. |

This module stores the acceptance workflow without pre-filling the current round's traffic population. The API request still determines the population and slice mix. Existing configuration schemas, journal fields, and restoration order remain.

The CLIs are now `python -m backend.onprem.validation.normal ...` and `python -m backend.onprem.validation.constrained ...`. `test/ai100_live_e2e.py` uses the new paths. Moving files and passing API regression do not establish live QER effect acceptance.

After a normal scenario completes, the runner uses `normal release-normal` against the ledger's actual closure preparation record. If a claim exists, the native cohort identity must still match. If registration fails or the round is canceled before the claim is established, the runner checks the original request, allocation ownership, verified round cleanup, traffic-window cleanup, and absence of policy writes. It saves a separate `retirement` record with `claim=null`. It does not require a failed UE to establish a PDU first or claim AI or QER effect evidence.

The coordinator establishes a normal or constrained scenario claim before measurement for populations of 1–100 UEs. API regression includes 3-UE and 50-UE cases and cancellation after partial registration followed by a new request.

If admission explicitly returns 503 and no round was created, the runner uses `normal withdraw-normal`. This operation requires native zero-UE evidence and confirms that no durable round contains the request ID. It closes only the unused preparation record; it neither changes the network nor turns a rejection into a pass.
