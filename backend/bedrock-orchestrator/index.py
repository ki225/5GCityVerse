"""
Bedrock Orchestrator Lambda
───────────────────────────
Triggered by EventBridge rule on CityEventTriggered.
Invokes Bedrock Supervisor Agent with city event context,
streams the agent's decisions, and updates DynamoDB + WebSocket.

AWS Bedrock Agents: Agent → Action Groups (Lambda tools) → NEF API
"""

import json
import os
import boto3
from datetime import datetime, timezone

bedrock_agent_rt = boto3.client('bedrock-agent-runtime')
dynamodb         = boto3.resource('dynamodb')
apigw_mgmt       = boto3.client(
    'apigatewaymanagementapi',
    endpoint_url=os.environ.get('APIGW_WS_ENDPOINT', ''),
)

TABLE_NAME    = os.environ['DYNAMODB_TABLE']
AGENT_ID      = os.environ['BEDROCK_AGENT_ID']
AGENT_ALIAS   = os.environ['BEDROCK_AGENT_ALIAS_ID']


def lambda_handler(event: dict, _context) -> None:
    detail = event.get('detail', {})
    execution_id = detail.get('execution_id', '')
    event_type   = detail.get('event_type', '')
    config       = detail.get('config', {})

    # Build the natural-language prompt for the Supervisor Agent
    prompt = _build_prompt(event_type, config)

    # Invoke Bedrock Agent (streaming)
    response = bedrock_agent_rt.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=AGENT_ALIAS,
        sessionId=execution_id,
        inputText=prompt,
        enableTrace=True,
    )

    # Collect streamed chunks and traces
    full_output = ''
    traces = []

    for chunk in response.get('completion', []):
        if 'chunk' in chunk:
            text = chunk['chunk'].get('bytes', b'').decode('utf-8')
            full_output += text
        if 'trace' in chunk:
            traces.append(chunk['trace'])

    # Parse structured decision from agent output
    decision = _parse_decision(full_output, event_type)

    # Persist
    table = dynamodb.Table(TABLE_NAME)
    table.update_item(
        Key={'pk': f'EVENT#{execution_id}', 'sk': 'STATUS'},
        UpdateExpression='SET agent_decision = :d, #st = :s, completed_at = :t',
        ExpressionAttributeNames={'#st': 'status'},
        ExpressionAttributeValues={
            ':d': decision,
            ':s': 'AGENT_COMPLETE',
            ':t': datetime.now(timezone.utc).isoformat(),
        },
    )

    # Push decision to all connected WebSocket clients
    _broadcast_ws({
        'type':    'agent_decision',
        'payload': {
            'agentName':       'Supervisor Agent',
            'riskLevel':       decision.get('risk_level', 'medium'),
            'decision':        decision.get('decision', full_output[:200]),
            'actions':         decision.get('actions', []),
            'expectedOutcome': decision.get('expected_outcome', ''),
            'score':           decision.get('score', 80),
            'startedAt':       detail.get('timestamp', ''),
        },
    })


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _build_prompt(event_type: str, config: dict) -> str:
    descriptions = {
        'concert':   f"A large AR concert is starting at the city stadium. {config.get('ue_count',50)} UEs are connecting for 4K/AR streaming. Expected eMBB traffic surge: {config.get('iperf_gbps',5)} Gbps.",
        'typhoon':   f"Typhoon alert: Category 5. Estimated {config.get('ue_count',200)} IoT emergency sensors activating. {config.get('ue_count',200)} UEs require URLLC emergency communications.",
        'accident':  f"Major traffic accident on the highway. {config.get('ue_count',20)} connected vehicles require V2X rerouting with ultra-low latency.",
        'medical':   f"Hospital ER utilization at 95%. Medical equipment requires URLLC slice priority for {config.get('ue_count',10)} critical devices.",
        'iot_surge': f"Massive IoT device registration surge: {config.get('ue_count',500)} sensors attempting simultaneous registration.",
    }

    return f"""You are a 5G smart city network management AI.

City event: {event_type}
Situation: {descriptions.get(event_type, 'Unknown event')}

Steps:
1. Call get_network_analytics to check current network state
2. Assess impact on network resources
3. Decide which network slices to activate/prioritize
4. Delegate to sub-agents for NEF API calls

Return a JSON object:
{{
  "risk_level": "low|medium|high|critical",
  "decision": "<explanation>",
  "actions": [
    {{
      "type": "nef_pfd|nef_traffic_influence|nef_qos|k8s_hpa",
      "description": "<what this does>",
      "api": "<endpoint>",
      "status": "pending"
    }}
  ],
  "expected_outcome": "<what will improve>",
  "score": <0-100>
}}"""


def _parse_decision(output: str, event_type: str) -> dict:
    """Try to extract JSON from agent output, fall back to default."""
    import re
    match = re.search(r'\{[\s\S]*\}', output)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Fallback defaults per event type
    defaults = {
        'concert':   {'risk_level': 'high',     'score': 88},
        'typhoon':   {'risk_level': 'critical',  'score': 95},
        'accident':  {'risk_level': 'high',      'score': 85},
        'medical':   {'risk_level': 'critical',  'score': 92},
        'iot_surge': {'risk_level': 'high',      'score': 80},
    }
    d = defaults.get(event_type, {'risk_level': 'medium', 'score': 75})
    return {
        'risk_level':       d['risk_level'],
        'decision':         output[:300] if output else 'Analysis complete.',
        'actions':          [],
        'expected_outcome': 'Network resources optimized for event.',
        'score':            d['score'],
    }


def _broadcast_ws(message: dict) -> None:
    """Push message to all connected WebSocket clients (stored in DynamoDB)."""
    table = dynamodb.Table(TABLE_NAME)
    connections = table.query(
        IndexName='gsi-connections',
        KeyConditionExpression='pk = :pk',
        ExpressionAttributeValues={':pk': 'WS_CONNECTION'},
    ).get('Items', [])

    payload = json.dumps(message).encode('utf-8')
    for conn in connections:
        try:
            apigw_mgmt.post_to_connection(
                ConnectionId=conn['sk'],
                Data=payload,
            )
        except apigw_mgmt.exceptions.GoneException:
            # Stale connection — clean up
            table.delete_item(Key={'pk': 'WS_CONNECTION', 'sk': conn['sk']})
