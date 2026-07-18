"""
Bedrock Orchestrator Lambda

Triggered by EventBridge rule on CityEventTriggered.
Invokes Bedrock Supervisor Agent with city event context,
streams the agent's decisions, and updates DynamoDB + WebSocket.

AWS Bedrock Agents: Agent → Action Groups (Lambda tools) → NEF API
"""

import json
import os
import boto3
from datetime import datetime, timezone

from prompts import SupervisorPromptBuilder

bedrock_agent_rt = boto3.client('bedrock-agent-runtime')
dynamodb         = boto3.resource('dynamodb')
apigw_mgmt       = boto3.client(
    'apigatewaymanagementapi',
    endpoint_url=os.environ.get('APIGW_WS_ENDPOINT', ''),
)

TABLE_NAME    = os.environ['DYNAMODB_TABLE']
AGENT_ID      = os.environ['BEDROCK_AGENT_ID']
AGENT_ALIAS   = os.environ['BEDROCK_AGENT_ALIAS_ID']
PROMPT_BUILDER = SupervisorPromptBuilder()


def lambda_handler(event: dict, _context) -> None:
    detail = event.get('detail', {})
    execution_id = detail.get('execution_id', '')
    event_type   = detail.get('event_type', '')
    config       = detail.get('config', {})

    prompt = PROMPT_BUILDER.build(event_type, config)

    response = bedrock_agent_rt.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=AGENT_ALIAS,
        sessionId=execution_id,
        inputText=prompt,
        enableTrace=True,
    )

    full_output = ''
    traces = []

    for chunk in response.get('completion', []):
        if 'chunk' in chunk:
            text = chunk['chunk'].get('bytes', b'').decode('utf-8')
            full_output += text
        if 'trace' in chunk:
            traces.append(chunk['trace'])

    decision = _parse_decision(full_output, event_type)

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

    _broadcast_ws({
        'type':    'agent_decision',
        'payload': {
            'agentName':       'Supervisor Agent',
            'riskLevel':       decision.get('risk_level', 'medium'),
            'decision':        decision.get('decision', full_output[:200]),
            'actions':         decision.get('actions', []),
            'expectedOutcome': decision.get('expected_outcome', ''),
            'startedAt':       detail.get('timestamp', ''),
        },
    })


def _parse_decision(output: str, event_type: str) -> dict:
    """Try to extract JSON from agent output, fall back to default."""
    import re
    match = re.search(r'\{[\s\S]*\}', output)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Fallback defaults per event type.
    defaults = {
        'concert':   {'risk_level': 'high'},
        'typhoon':   {'risk_level': 'critical'},
        'accident':  {'risk_level': 'high'},
        'medical':   {'risk_level': 'critical'},
        'iot_surge': {'risk_level': 'high'},
    }
    d = defaults.get(event_type, {'risk_level': 'medium'})
    return {
        'risk_level':       d['risk_level'],
        'decision':         output[:300] if output else 'Analysis complete.',
        'actions':          [],
        'expected_outcome': 'Network resources optimized for event.',
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
            # Stale connection; clean up.
            table.delete_item(Key={'pk': 'WS_CONNECTION', 'sk': conn['sk']})
