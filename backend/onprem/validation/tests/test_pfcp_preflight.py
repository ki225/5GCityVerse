import unittest
import gzip
from backend.onprem.operations.diagnostics.pfcp_preflight import association_proof, retained_log_text, retained_log_path

class AssociationTests(unittest.TestCase):
    smf = "2026-09-27T21:19:38Z Received PFCP Association Setup Accepted Response from UPF[172.22.4.21]"
    upf = "2026-09-27T21:19:38Z [CPNodeID:172.22.4.10] New node"
    def test_current_pod_epochs_are_required(self):
        proof = association_proof(self.smf, self.upf, "172.22.4.21", "2026-09-27T21:19:37Z", "2026-09-27T20:57:00Z")
        self.assertIn("acceptedAt", proof)
        with self.assertRaisesRegex(ValueError, "unproven"):
            association_proof(self.smf, self.upf, "172.22.4.21", "2026-09-27T21:19:37Z", "2026-09-27T21:20:00Z")
    def test_smf_only_or_later_upf_rejection_never_proves_association(self):
        for upf in ("", self.upf + "\n2026-09-27T21:20:00Z not found NodeID 172.22.4.10"):
            with self.assertRaises(ValueError):
                association_proof(self.smf, upf, "172.22.4.21", "2026-09-27T21:19:37Z", "2026-09-27T20:57:00Z")

    def test_retained_history_recovers_rotated_setup_and_keeps_later_failure(self):
        listing = '<a href="0.log">0.log</a><a href="0.log.20260927-212750">old</a>'
        files = {"0.log.20260927-212750": "2026-09-27T20:57:01Z stdout F startup\n2026-09-27T21:19:38Z stdout F " + self.upf,
                 "0.log": "2026-09-27T21:28:00Z stdout F 2026-09-27T21:28:00Z report"}
        log = retained_log_text(listing, files.__getitem__, 0, "2026-09-27T20:57:00Z")
        association_proof(self.smf, log, "172.22.4.21", "2026-09-27T21:19:37Z", "2026-09-27T20:57:00Z")
        files["0.log"] += "\n2026-09-27T21:29:00Z stdout F 2026-09-27T21:29:00Z not found NodeID 172.22.4.10"
        with self.assertRaisesRegex(ValueError, "invalidated"):
            association_proof(self.smf, retained_log_text(listing, files.__getitem__, 0, "2026-09-27T20:57:00Z"),
                              "172.22.4.21", "2026-09-27T21:19:37Z", "2026-09-27T20:57:00Z")

    def test_retained_logs_do_not_cross_uid_or_restart_or_missing_start(self):
        identity = {"node":"cv5g-upf", "pod":"upf-embb-a", "uid":"uid-one"}
        first = retained_log_path("free5gc-onprem", identity, "upf-embb")
        second = retained_log_path("free5gc-onprem", {**identity,"uid":"uid-two"}, "upf-embb")
        self.assertNotEqual(first, second)
        self.assertIn("_uid-one/", first)
        listing = '<a href="0.log">0.log</a>'
        with self.assertRaisesRegex(ValueError, "log set"):
            retained_log_text(listing, lambda _: "", 1, "2026-09-27T20:57:00Z")
        with self.assertRaisesRegex(ValueError, "startup"):
            retained_log_text(listing, lambda _: "2026-09-27T21:28:00Z stdout F late", 0, "2026-09-27T20:57:00Z")

    def test_compressed_retained_history_is_decoded_with_same_epoch_checks(self):
        listing = '<a href="0.log">current</a><a href="0.log.20260927-212536.gz">old</a>'
        files = {"0.log": "2026-09-27T21:29:00Z stdout F current",
                 "0.log.20260927-212536.gz": gzip.compress(b"2026-09-27T20:57:01Z stdout F startup")}
        text = retained_log_text(listing, files.__getitem__, 0, "2026-09-27T20:57:00Z")
        self.assertEqual(text, "startup\ncurrent")

    def test_upf_retained_window_may_start_at_current_smf_epoch(self):
        listing = '<a href="0.log">current</a><a href="0.log.20260927-211936">old</a>'
        files = {
            "0.log.20260927-211936": (
                "2026-09-27T21:19:36Z stdout F before floor\n"
                "2026-09-27T21:19:38Z stdout F " + self.upf + "\n"
                "2026-09-27T21:19:43Z stdout F heartbeat"),
            "0.log": (
                "2026-09-27T21:19:48Z stdout F heartbeat\n"
                "2026-09-27T21:19:53Z stdout F heartbeat"),
        }
        text = retained_log_text(
            listing, files.__getitem__, 0, "2026-09-26T20:57:00Z",
            coverage_floor="2026-09-27T21:19:37Z",
            coverage_end="2026-09-27T21:19:54Z")
        association_proof(
            self.smf, text, "172.22.4.21",
            "2026-09-27T21:19:37Z", "2026-09-26T20:57:00Z")
        files["0.log"] += (
            "\n2026-09-27T21:19:53.500000Z stdout F "
            "2026-09-27T21:19:53.500000Z not found NodeID 172.22.4.10")
        with self.assertRaisesRegex(ValueError, "invalidated"):
            association_proof(
                self.smf,
                retained_log_text(
                    listing, files.__getitem__, 0, "2026-09-26T20:57:00Z",
                    coverage_floor="2026-09-27T21:19:37Z",
                    coverage_end="2026-09-27T21:19:54Z"),
                "172.22.4.21", "2026-09-27T21:19:37Z", "2026-09-26T20:57:00Z")

    def test_upf_retained_window_rejects_floor_end_and_middle_gaps(self):
        listing = '<a href="0.log">current</a><a href="0.log.20260927-211936">old</a>'
        cases = (
            {"0.log.20260927-211936": "2026-09-27T21:19:43Z stdout F late",
             "0.log": "2026-09-27T21:19:48Z stdout F current"},
            {"0.log.20260927-211936": "2026-09-27T21:19:36Z stdout F early",
             "0.log": "2026-09-27T21:19:43Z stdout F stale"},
            {"0.log.20260927-211936": "2026-09-27T21:19:36Z stdout F early",
             "0.log": "2026-09-27T21:20:00Z stdout F after missing middle"},
        )
        for files in cases:
            with self.subTest(files=files), self.assertRaisesRegex(
                    ValueError, "cover|snapshot|gap"):
                retained_log_text(
                    listing, files.__getitem__, 0, "2026-09-26T20:57:00Z",
                    coverage_floor="2026-09-27T21:19:37Z",
                    coverage_end="2026-09-27T21:19:54Z")

    def test_smf_retained_history_still_requires_its_own_startup(self):
        listing = '<a href="0.log">current</a>'
        with self.assertRaisesRegex(ValueError, "startup"):
            retained_log_text(
                listing,
                lambda _: "2026-09-27T21:19:43Z stdout F late",
                0, "2026-09-27T21:19:37Z")
