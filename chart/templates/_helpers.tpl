{{/* Prefix for all resource names. Components append -gateway, -vllm, ... */}}
{{- define "tlg.fullname" -}}
{{- default .Release.Name .Values.fullnameOverride | trunc 50 | trimSuffix "-" -}}
{{- end }}

{{- define "tlg.labels" -}}
app.kubernetes.io/name: tiny-llm-gateway
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{/* Usage: include "tlg.selectorLabels" (dict "ctx" $ "component" "gateway") */}}
{{- define "tlg.selectorLabels" -}}
app.kubernetes.io/name: tiny-llm-gateway
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "tlg.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end }}

{{/*
A secret value: the explicitly configured one, else the value already stored
in the release's Secret (so upgrades keep it), else a new random one.
Usage: include "tlg.secretValue" (dict "ctx" $ "key" "master-key" "value" .Values.x)
*/}}
{{- define "tlg.secretValue" -}}
{{- $existing := lookup "v1" "Secret" .ctx.Release.Namespace (include "tlg.fullname" .ctx) -}}
{{- if .value -}}
{{ .value }}
{{- else if and $existing $existing.data (hasKey $existing.data .key) -}}
{{ index $existing.data .key | b64dec }}
{{- else -}}
{{ randAlphaNum 32 }}
{{- end -}}
{{- end }}

{{/* Model served by the "local" backend in routes. */}}
{{- define "tlg.localModel" -}}
{{- if eq .Values.localBackend "vllm" }}{{ .Values.vllm.model }}{{ else }}mock-qwen3{{ end -}}
{{- end }}

{{/* The gateway's routes.yaml, with templates in .Values.routes rendered. */}}
{{- define "tlg.routes" -}}
{{- if not (has .Values.localBackend (list "vllm" "mock")) }}
{{- fail (printf "localBackend must be vllm or mock, not %q" (toString .Values.localBackend)) }}
{{- end }}
{{- tpl (toYaml .Values.routes) . }}
{{- end }}

{{/* Secret and key holding the OpenAI API key (may not exist: the env var is optional). */}}
{{- define "tlg.openaiSecretName" -}}
{{- .Values.openai.existingSecret | default (include "tlg.fullname" .) -}}
{{- end }}
{{- define "tlg.openaiSecretKey" -}}
{{- if .Values.openai.existingSecret }}{{ .Values.openai.existingSecretKey }}{{ else }}openai-api-key{{ end -}}
{{- end }}

{{/* Prometheus configuration (scrape targets). */}}
{{- define "tlg.prometheusConfig" -}}
global:
  scrape_interval: {{ .Values.prometheus.scrapeInterval }}
scrape_configs:
  # One target per gateway replica, found via the headless Service.
  - job_name: gateway
    dns_sd_configs:
      - names: ["{{ include "tlg.fullname" . }}-gateway-pods.{{ .Release.Namespace }}.svc"]
        type: A
        port: 8080
        refresh_interval: 15s
  {{- if eq .Values.localBackend "vllm" }}
  # vLLM's own metrics: queue length, KV cache usage, tokens per second, ...
  - job_name: vllm
    static_configs:
      - targets: ["{{ include "tlg.fullname" . }}-vllm:8000"]
  {{- end }}
{{- end }}
