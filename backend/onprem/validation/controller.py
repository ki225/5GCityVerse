"""Trusted staged execution of a controlled QER fixture.

The fixture installation is separate from model policy actions. Every network
write has a durable one-use intent; failure leaves the global guard active.
"""
from __future__ import annotations

from backend.onprem.validation.constrained import activate_constrained, begin_fixture_install_intent, read_constrained_receipt, seal_fixture_installed
from backend.onprem.validation.native import seal_native_receipt
from backend.onprem.validation import normal as common


def install_constrained_fixture(provider, journal_path, snapshot_reader, claim, *,
                                expected_config_sha256, expected_quarantine_registry,
                                check_cancel):
    """Collect original state, install 100 exact caps and seal native readback.

    The caller must have published the sealed claim to the owning RoundLedger.
    A rejected, lost or ambiguous write is terminal for this attempt.
    """
    if not callable(snapshot_reader) or not callable(check_cancel):
        raise ValueError("Fixture runtime sources are unavailable")
    check_cancel()
    snapshot = snapshot_reader()
    if (snapshot.get("requestId") != claim.get("requestId")
            or snapshot.get("roundId") != claim.get("roundId")
            or snapshot.get("generation") != claim.get("generation")):
        raise ValueError("Fixture round changed before original read")
    published = [item for item in snapshot.get("actions", [])
                 if type(item) is dict and item.get("kind") == "scenario_validation_track"]
    if (len(published) != 1 or published[0].get("accepted") is not True
            or published[0].get("fixtureClaim") != claim):
        raise ValueError("Fixture claim was not published by the owning round")
    original_dumps = {}
    original_proof = provider.native(snapshot, _fixture_raw_sink=original_dumps)
    original = seal_native_receipt(snapshot, claim, original_proof, original_dumps, "original")
    activate_constrained(journal_path, snapshot, original,
                         expected_quarantine_registry=expected_quarantine_registry)
    batch = provider.begin_fixture_batch(
        journal_path, snapshot, expected_config_sha256=expected_config_sha256,
        expected_quarantine_registry=expected_quarantine_registry)
    try:
        for owner in sorted(snapshot["perUe"], key=lambda row: row["supi"]):
            check_cancel()
            current = snapshot_reader()
            intent = begin_fixture_install_intent(
                journal_path, current, owner["supi"],
                expected_config_sha256=expected_config_sha256,
                expected_quarantine_registry=expected_quarantine_registry)
            provider.request_fixture_install(
                journal_path, current, intent["intentId"], batch_authority=batch,
                expected_config_sha256=expected_config_sha256,
                expected_quarantine_registry=expected_quarantine_registry)
    finally:
        provider.end_fixture_batch(batch)
    check_cancel()
    current = snapshot_reader()
    installed_dumps = {}
    installed_proof = provider.native(current, _fixture_raw_sink=installed_dumps)
    installed = seal_native_receipt(current, claim, installed_proof, installed_dumps,
                                    "fixture_installed")
    digest = seal_fixture_installed(
        journal_path, current, installed,
        expected_quarantine_registry=expected_quarantine_registry)
    record = read_constrained_receipt(journal_path, claim["requestId"])
    if record["state"] != "fixture_installed":
        raise ValueError("Fixture native seal was not durable")
    return {"claim": claim, "originalNativeSha256": common._digest(record["originalNative"]),
            "fixtureInstalledNativeSha256": digest}


def restore_constrained_fixture(provider, journal_path, snapshot_reader, claim, *,
                                expected_config_sha256, expected_quarantine_registry,
                                check_cancel):
    """Restore each UE's archived original rate after model cleanup is sealed.

    A failed or uncertain response leaves the durable fixture guard active.
    This function never guesses a nominal original rate or resends an intent.
    """
    from backend.onprem.validation.constrained import constrained_claim_for_snapshot
    from backend.onprem.validation.restore import accepted_restore_supis, seal_original_restored
    if not callable(snapshot_reader) or not callable(check_cancel):
        raise ValueError("Fixture restoration has no trusted round source")
    check_cancel()
    snapshot = snapshot_reader()
    if (snapshot.get("requestId") != claim.get("requestId")
            or constrained_claim_for_snapshot(
                journal_path, snapshot,
                expected_quarantine_registry=expected_quarantine_registry) != claim):
        raise ValueError("Fixture restoration changed owner")
    record = read_constrained_receipt(journal_path, snapshot["requestId"])
    if record["state"] != "inner_restored":
        raise ValueError("Model cleanup lacks its inner native seal")
    previously_accepted = accepted_restore_supis(journal_path, snapshot)
    published = [item for item in snapshot.get("actions", [])
                 if type(item) is dict and item.get("kind") == "scenario_validation_track"]
    if (len(published) != 1 or published[0].get("accepted") is not True
            or published[0].get("fixtureClaim") != claim):
        raise ValueError("Fixture restore claim was not published by this round")
    batch = provider.begin_fixture_batch(
        journal_path, snapshot, expected_config_sha256=expected_config_sha256,
        expected_quarantine_registry=expected_quarantine_registry,
        phase="fixture_restore")
    try:
        for owner in sorted(record["originalNative"]["native"]["perUe"],
                            key=lambda row: row["supi"]):
            if owner["supi"] in previously_accepted:
                continue
            check_cancel()
            current = snapshot_reader()
            intent = provider.begin_fixture_restore_intent(
                journal_path, current, owner["supi"], batch_authority=batch,
                expected_quarantine_registry=expected_quarantine_registry)
            provider.request_fixture_restore(
                journal_path, current, intent["intentId"], batch_authority=batch,
                expected_config_sha256=expected_config_sha256,
                expected_quarantine_registry=expected_quarantine_registry)
    finally:
        provider.end_fixture_batch(batch)
    check_cancel()
    current = snapshot_reader()
    if constrained_claim_for_snapshot(
            journal_path, current,
            expected_quarantine_registry=expected_quarantine_registry) != claim:
        raise ValueError("Fixture owner changed before original readback")
    restored_dumps = {}
    restored_proof = provider.native(current, _fixture_raw_sink=restored_dumps)
    restored = seal_native_receipt(current, claim, restored_proof, restored_dumps,
                                   "original_restored")
    digest = seal_original_restored(
        journal_path, current, restored,
        expected_quarantine_registry=expected_quarantine_registry)
    final = read_constrained_receipt(journal_path, current["requestId"])
    if final["state"] != "original_restored":
        raise ValueError("Original native QER restoration was not sealed")
    return {"claim": claim, **digest,
            "acceptedRestoreCount": len(record["originalNative"]["native"]["perUe"])}
