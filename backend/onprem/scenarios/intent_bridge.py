"""Separate formal service control from the legacy holdout diagnostic experiment."""
import os

from .contracts import ScenarioRequestError, validate_scenario_request


DIAGNOSTIC_ENV = 'CITYVERSE_LEGACY_AI_DIAGNOSTICS'


def diagnostics_enabled():
    return os.environ.get(DIAGNOSTIC_ENV) == 'true'


def require_diagnostic_strategy(body):
    if body.get('strategy') == 'ai' and not diagnostics_enabled():
        raise ScenarioRequestError('NEEDS_SERVICE_INTENT',
            'Formal AI control requires an explicit service intent. Submit the canonical intent '
            'through /api/intents, or include intent with this traffic request. '
            'The legacy treatment/holdout experiment is disabled.')


def strategy_capabilities():
    return {'formalAiEndpoint': '/api/intents', 'formalAiRequiresContract': True,
            'legacyAiDiagnostic': diagnostics_enabled(),
            'legacyAiDiagnosticLabel': 'Treatment/holdout diagnostic experiment; not formal all-member service control'}


class CityIntentBridge:
    def __init__(self, context, scenarios):
        self.context, self.scenarios = context, scenarios

    async def submit(self, payload, owner, key):
        from backend.onprem.intents.domain import ContractError, ServiceIntent
        body = dict(payload)
        raw = body.pop('intent')
        intent = ServiceIntent.from_dict(raw)
        service = self.context.intent_service
        issues = intent.definition_issues(service.authority)
        if issues:
            raise ContractError('needs_definition:' + ','.join(issues))
        if not intent.is_active(service.clock()):
            raise ContractError('intent_not_active')
        # A formal city event supplies real traffic; only IntentService may alter policy.
        body['strategy'] = 'none'
        validate_scenario_request(body, allow_none=True)
        # Resolve commissioned radio access before submitting an operation that
        # may provision native resources. The scenario service repeats this
        # check under its admission fence before launching UEs.
        if body.get('demandGroups') is not None:
            await self.scenarios.prepare_spatial(body)
        from backend.onprem.traffic.admission import admit_traffic
        admit_traffic(body)
        operation = await service.submit({'intent':intent.to_dict(), 'city_traffic':body}, owner, key)
        if operation.get('traffic_start_mode') == 'runtime':
            return {'schemaVersion':'cityverse.city-intent-submission/v1','intent':operation,
                'traffic':{'status':'queued','requestId':body['requestId']},
                'controlPath':'service-intent','serviceEffectVerified':False,
                'nextStep':'Native admission and selection must be installed before the runtime starts city traffic'}
        try:
            from .intent_traffic import ServiceIntentTrafficLease
            lease = ServiceIntentTrafficLease.issue(intent, owner, operation['operation_id'], body['requestId'])
            traffic = await self.scenarios.start_intent_traffic(body, lease)
        except Exception:
            await service.abort_before_traffic(operation['operation_id'], owner)
            raise
        await service.confirm_traffic_admission(operation['operation_id'], owner,
            {**traffic, 'requestId':body['requestId'], 'accepted':True,
             'source':'city-scenario-admission'})
        return {'schemaVersion': 'cityverse.city-intent-submission/v1', 'intent': operation,
                'traffic': traffic, 'controlPath': 'service-intent',
                'serviceEffectVerified': False,
                'nextStep': 'Monitor the intent operation; configuration and service effect require current receiver evidence'}
