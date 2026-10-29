{{/* Only placement and schema validation; API behaviour belongs to backend/. */}}
{{- define "cityverse.selector" -}}
{{- $root := index . 0 -}}
{{- $site := index . 1 -}}
{{- if eq $root.Values.city.placement.mode "colocated" -}}
{{- required "city.placement.nodeSelector is required for colocated mode" $root.Values.city.placement.nodeSelector | toYaml -}}
{{- else if eq $root.Values.city.placement.mode "physical" -}}
{{- dict "cityverse.io/site" $site | toYaml -}}
{{- else -}}
{{- fail "city.placement.mode must be physical or colocated" -}}
{{- end -}}
{{- end -}}

{{/* Read-only Pod-local endpoint; implementation remains in the backend-owned source CM. */}}
{{- define "cityverse.ranEvidenceContainer" -}}
{{- $root := index . 0 -}}
{{- $role := index . 1 -}}
name: {{ $role }}-evidence
image: {{ $root.Values.images.ranEvidence }}
imagePullPolicy: IfNotPresent
workingDir: /opt/cityverse
command: [python, -m, backend.onprem.api.evidence_endpoint]
args: [--role, {{ $role }}, --host, "$(POD_IP)", --port, "9083"]
env:
  - {name: POD_IP, valueFrom: {fieldRef: {fieldPath: status.podIP}}}
  - {name: POD_UID, valueFrom: {fieldRef: {fieldPath: metadata.uid}}}
  - {name: PYTHONDONTWRITEBYTECODE, value: "1"}
ports: [{name: ran-evidence, containerPort: 9083, protocol: TCP}]
securityContext:
  runAsNonRoot: true
  runAsUser: 65532
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: {drop: [ALL]}
resources: {{ $root.Values.nativeRuntime.ranEvidence.resources | toYaml | nindent 2 }}
readinessProbe: {httpGet: {path: /healthz, port: 9083}, periodSeconds: 5, timeoutSeconds: 1}
volumeMounts:
  - {name: ran-evidence-source, mountPath: /opt/cityverse/backend, readOnly: true}
  {{- if eq $role "gnb" }}
  - {name: config, mountPath: /etc/cityverse, readOnly: true}
  {{- end }}
{{- end -}}

{{- define "cityverse.ranSelector" -}}
{{- $root := index . 0 -}}
{{- $gnb := index . 1 -}}
{{- if eq $root.Values.city.placement.mode "colocated" -}}
{{- include "cityverse.selector" (list $root "") -}}
{{- else -}}
{{- default (dict "cityverse.io/role" "ran") $gnb.nodeSelector | toYaml -}}
{{- end -}}
{{- end -}}

{{- define "cityverse.gnbConfig" -}}
{{- $root := index . 0 -}}
{{- $gnb := index . 1 -}}
{{- $slice := index $root.Values.city.slices $gnb.slice -}}
{{- dict "gnb" (dict
  "amfN2Ip" "172.22.2.10" "ranN2Ip" (printf "172.22.2.%d" (add 30 $gnb.networkIndex))
  "upfN3Ip" (printf "%s.10" $slice.n3Subnet) "ranN3Ip" (printf "%s.%d" $slice.n3Subnet (int $gnb.n3Host))
  "ranControlPlaneIp" "0.0.0.0" "ranDataPlaneIp" "0.0.0.0"
  "amfN2Port" 38412 "ranN2Port" 38413 "upfN3Port" 2152 "ranN3Port" 2152
  "ranControlPlanePort" 31413 "ranDataPlanePort" 31414
  "gnbId" (printf "00035%d" (int $gnb.networkIndex)) "gnbName" (printf "cv-fru-%s" (trimPrefix "gnb-" $gnb.workload))
  "plmnId" (dict "mcc" "208" "mnc" "93")
  "tai" (dict "tac" "000001" "broadcastPlmnId" (dict "mcc" "208" "mnc" "93"))
  "snssai" (dict "sst" (toString $slice.sst) "sd" $slice.sd)
  "api" (dict "ip" "127.0.0.1" "port" 40104)) "logger" (dict "level" "info") | toJson -}}
{{- end -}}
