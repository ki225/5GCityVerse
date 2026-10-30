import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.onprem import check_fru_zero as guard

residuals = guard.residuals


class FreeRanZeroGuardTests(unittest.TestCase):
    def test_clean_core_objects_pass(self):
        self.assertEqual(residuals([
            {'kind': 'Service', 'metadata': {'name': 'smf', 'labels': {}}},
            {'kind': 'ConfigMap', 'metadata': {'name': 'nssf-config', 'labels': {}}},
        ]), [])

    def test_service_and_configmap_residuals_fail_even_after_pods_disappear(self):
        self.assertEqual(residuals([
            {'kind': 'Service', 'metadata': {'name': 'cv-fru-gnb-urllc',
                                             'labels': {'cityverse.io/managed-by': 'fru-benchmark'}}},
            {'kind': 'ConfigMap', 'metadata': {'name': 'cv-fru-gnb-urllc',
                                               'labels': {'cityverse.io/managed-by': 'fru-benchmark'}}},
        ]), ['ConfigMap/cv-fru-gnb-urllc', 'Service/cv-fru-gnb-urllc'])

    def test_missing_label_or_lock_is_still_residual(self):
        self.assertEqual(residuals([
            {'kind': 'Service', 'metadata': {'name': 'cv-fru-gnb-embb', 'labels': {}}},
            {'kind': 'ConfigMap', 'metadata': {'name': 'cv-fru-benchmark-lock'}},
        ]), ['ConfigMap/cv-fru-benchmark-lock', 'Service/cv-fru-gnb-embb'])

    def test_job_and_deployment_residuals_fail_even_without_pods(self):
        self.assertEqual(residuals([
            {'kind': 'Job', 'metadata': {'name': 'cv-fru-cleanup', 'labels': {}}},
            {'kind': 'Deployment', 'metadata': {'name': 'gnb-embb',
                                               'labels': {'cityverse.io/managed-by': 'fru-benchmark'}}},
        ]), ['Deployment/gnb-embb', 'Job/cv-fru-cleanup'])

    def test_live_guard_refuses_service_after_ue_pods_are_zero(self):
        response = {'items': [{'kind': 'Service', 'metadata': {
            'name': 'cv-fru-gnb-urllc', 'labels': {'cityverse.io/managed-by': 'fru-benchmark'}}}]}
        with patch.object(Path, 'is_file', return_value=True), patch.object(
                guard.subprocess, 'check_output', return_value=json.dumps(response)) as call:
            with self.assertRaisesRegex(RuntimeError, 'Service/cv-fru-gnb-urllc'):
                guard.check(Path('kubectl.exe'), Path('kubeconfig'))
        self.assertIn('pods,jobs,deployments,services,configmaps', call.call_args.args[0])

    def test_clean_scope_is_explicitly_only_kubernetes_objects(self):
        with patch.object(Path, 'is_file', return_value=True), patch.object(
                guard.subprocess, 'check_output', return_value=json.dumps({'items': []})):
            result = guard.check(Path('kubectl.exe'), Path('kubeconfig'))
        self.assertEqual(result['fruResiduals'], 0)
        self.assertEqual(result['scope'], 'fru-kubernetes-objects-only')


if __name__ == '__main__':
    unittest.main()
