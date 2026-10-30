"""Per-call fixed reservation failure codes; no remote text or shared state."""
from backend.onprem.runtime.errors import EvidenceUnavailable

MESSAGES = {
 'Durable UE retirement quarantine blocks controller mutation':'retirement_quarantine',
 'needs_measurement:commissioned_ue_launch_profile_missing':'launch_profile_missing',
 'needs_measurement:ue_launch_profile_differs_from_driver':'initializer_profile_mismatch',
 'needs_measurement:multi_session_driver_profile_missing':'device_profile_mismatch',
 'Empty native fleet is required before reserving new sessions':'canonical_fleet_not_empty',
 'needs_measurement:fresh_empty_native_fleet_required':'fresh_empty_fleet_unconfirmed',
}
CODES=frozenset(MESSAGES.values()) | {'native_evidence_unavailable'}

def annotate(error):
    if type(error) is EvidenceUnavailable:
        error.reservationDiagnostic={'schemaVersion':'cityverse.reservation-diagnostic/v1',
            'reasonCode':MESSAGES.get(str(error),'native_evidence_unavailable')}
    return error

def project(error):
    value=getattr(error,'reservationDiagnostic',None)
    if (type(error) is EvidenceUnavailable and type(value) is dict
            and value.get('schemaVersion')=='cityverse.reservation-diagnostic/v1'
            and value.get('reasonCode') in CODES):
        return {'schemaVersion':value['schemaVersion'],'reasonCode':value['reasonCode']}
    return None
