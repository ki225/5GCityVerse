from __future__ import annotations


class SupervisorPromptBuilder:
    def build(self, event_type: str, config: dict) -> str:
        descriptions = {
            'concert': f"A large AR concert is starting at the city stadium. {config.get('ue_count',50)} UEs are connecting for 4K/AR streaming. Expected eMBB traffic surge: {config.get('iperf_gbps',5)} Gbps.",
            'typhoon': f"Typhoon alert: Category 5. Estimated {config.get('ue_count',200)} IoT emergency sensors activating. {config.get('ue_count',200)} UEs require URLLC emergency communications.",
            'accident': f"Major traffic accident on the highway. {config.get('ue_count',20)} connected vehicles require V2X rerouting with ultra-low latency.",
            'medical': f"Hospital ER utilization at 95%. Medical equipment requires URLLC slice priority for {config.get('ue_count',10)} critical devices.",
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
  "expected_outcome": "<what will improve>"
}}"""
