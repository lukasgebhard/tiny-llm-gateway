#!/usr/bin/env bash
# End-to-end demo on minikube: local model, external model, per-key access
# policy, automatic fallback when the local model server goes down, and the
# resulting usage report.
#
# Usage: scripts/demo.sh [--mock]
#   --mock          use the mock backend instead of vLLM (no model download)
#
# Environment:
#   OPENAI_API_KEY  enables the external backend (optional)
#   DEMO_PORT       local port for the gateway (default 8080)
#   DEMO_PROM_PORT  local port for Prometheus (default 9090)
#   DEMO_PAUSE=0    don't pause between steps
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
RELEASE=tlg
PORT=${DEMO_PORT:-8080}
BASE="http://localhost:$PORT"
PROM_PORT=${DEMO_PROM_PORT:-9090}
PROM="http://localhost:$PROM_PORT"
LOCAL_BACKEND=vllm
[[ ${1:-} == --mock ]] && LOCAL_BACKEND=mock
PROMPT="In one sentence: what does an LLM gateway do?"

step() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }
note() { printf '\033[2m%s\033[0m\n' "$*"; }
pause() { [[ ${DEMO_PAUSE:-1} == 0 ]] || read -rp $'\nPress Enter to continue...' _; }

for tool in minikube kubectl helm curl jq; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done

# Waits until the pods of a component exist and are ready.
wait_ready() {
  local selector="app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=$1"
  until kubectl get pods -l "$selector" -o name | grep -q .; do sleep 2; done
  kubectl wait --for=condition=ready pod -l "$selector" --timeout=20m >/dev/null
}

# Prints status, gateway headers and the answer (or error) of a chat completion.
chat() {
  local key=$1 model=$2 body hdr out status backend attempts
  body=$(jq -n --arg m "$model" --arg p "$PROMPT" \
    '{model: $m, max_tokens: 60, messages: [{role: "user", content: $p}]}')
  hdr=$(mktemp) out=$(mktemp)
  status=$(curl -s -o "$out" -D "$hdr" -w '%{http_code}' "$BASE/v1/chat/completions" \
    -H "Authorization: Bearer $key" -H 'Content-Type: application/json' -d "$body")
  backend=$(grep -i '^gateway-backend:' "$hdr" | cut -d' ' -f2 | tr -d '\r' || true)
  attempts=$(grep -i '^gateway-attempts:' "$hdr" | cut -d' ' -f2 | tr -d '\r' || true)
  echo "HTTP $status   Gateway-Backend: ${backend:--}   Gateway-Attempts: ${attempts:--}"
  if [[ $status == 200 ]]; then
    jq -r '"  [\(.model)] \(.choices[0].message.content)"' "$out"
  else
    jq -r '"  \(.error.code): \(.error.message)"' "$out"
  fi
  rm -f "$hdr" "$out"
}

# Prints the streamed answer token by token as it arrives.
stream() {
  local key=$1 model=$2 body
  body=$(jq -n --arg m "$model" --arg p "$PROMPT" \
    '{model: $m, stream: true, max_tokens: 60, messages: [{role: "user", content: $p}]}')
  printf '  '
  curl -sN "$BASE/v1/chat/completions" -H "Authorization: Bearer $key" \
    -H 'Content-Type: application/json' -d "$body" |
    while IFS= read -r line; do
      [[ $line == "data: {"* ]] || continue
      jq -rj '.choices[0].delta.content // empty' <<<"${line#data: }"
    done
  echo
}

create_key() {
  curl -s -X POST "$BASE/admin/keys" -H "Authorization: Bearer $MASTER_KEY" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg a "$1" --argjson e "$2" '{alias: $a, allow_external: $e}')" | jq -r .key
}

models() {
  curl -s "$BASE/v1/models" -H "Authorization: Bearer $1" | jq -r '[.data[].id] | join(", ")'
}

# Runs a PromQL query and prints one line per series: its labels and value.
promql() {
  printf '  \033[2m%s\033[0m\n' "$1"
  curl -s "$PROM/api/v1/query" --data-urlencode "query=$1" |
    jq -r 'if .data.result == [] then "    (no data)" else .data.result[] |
      "    \(.metric | del(.__name__) | to_entries | map("\(.key)=\(.value)") | join(" ")): \(.value[1])"
      end'
}

# Prints the usage report since the start of this run, grouped by key or backend.
usage_table() {
  local group=$1
  printf '  %-16s %8s %6s %9s %13s %17s %11s\n' "$group" requests errors fallbacks \
    prompt_tokens completion_tokens avg_latency
  curl -s "$BASE/admin/usage?group_by=$group&since=$SINCE" -H "Authorization: Bearer $MASTER_KEY" |
    jq -r --arg g "$group" '.data[] | [.[$g] // "-", .requests, .errors, .fallbacks,
      .prompt_tokens, .completion_tokens, "\(.avg_latency_ms) ms"] | @tsv' |
    while IFS=$'\t' read -r name requests errors fallbacks prompt completion latency; do
      printf '  %-16s %8s %6s %9s %13s %17s %11s\n' "$name" "$requests" "$errors" "$fallbacks" \
        "$prompt" "$completion" "$latency"
    done
}

step "1. Cluster and gateway image"
minikube status >/dev/null 2>&1 || minikube start --cpus=6 --memory=12g
BUILD_LOG=$(mktemp)
minikube image build -t tiny-llm-gateway:0.1.0 "$ROOT/gateway" >"$BUILD_LOG" 2>&1 ||
  { cat "$BUILD_LOG" >&2; echo "Image build failed" >&2; exit 1; }
rm -f "$BUILD_LOG"
note "Built tiny-llm-gateway:0.1.0 inside minikube."

step "2. Install the chart (local backend: $LOCAL_BACKEND)"
helm_args=(upgrade --install "$RELEASE" "$ROOT/chart" --reset-values --wait --timeout 20m)
[[ $LOCAL_BACKEND == mock ]] && helm_args+=(-f "$ROOT/chart/values-mock.yaml")
if [[ -n ${OPENAI_API_KEY:-} ]]; then
  kubectl create secret generic openai --from-literal=api-key="$OPENAI_API_KEY" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  helm_args+=(--set openai.existingSecret=openai)
else
  note "OPENAI_API_KEY is not set, so external models are disabled."
fi
[[ $LOCAL_BACKEND == vllm ]] && note "On the first run, vLLM downloads and compiles its model; this takes a few minutes."
helm "${helm_args[@]}" >/dev/null
# Undo a scale-down left over from an interrupted run, and restart the gateway so it
# picks up the freshly built image and starts with a clean cooldown state.
kubectl scale "deploy/$RELEASE-$LOCAL_BACKEND" --replicas=1 >/dev/null
kubectl rollout restart "deploy/$RELEASE-gateway" >/dev/null
kubectl rollout status "deploy/$RELEASE-gateway" --timeout=5m >/dev/null
wait_ready "$LOCAL_BACKEND"
kubectl get pods -l "app.kubernetes.io/instance=$RELEASE"
echo
kubectl port-forward "svc/$RELEASE-gateway" "$PORT:8080" >/dev/null 2>&1 &
PF_PID=$!
kubectl port-forward "svc/$RELEASE-prometheus" "$PROM_PORT:9090" >/dev/null 2>&1 &
PROM_PF_PID=$!
trap 'kill $PF_PID $PROM_PF_PID 2>/dev/null || true' EXIT
for _ in $(seq 30); do curl -sf "$BASE/healthz" >/dev/null && break; sleep 1; done
curl -sf "$BASE/healthz" >/dev/null ||
  { echo "Gateway not reachable on port $PORT (in use? set DEMO_PORT)" >&2; exit 1; }
MASTER_KEY=$(kubectl get secret "$RELEASE" -o jsonpath='{.data.master-key}' | base64 -d)
note "Gateway forwarded to $BASE"
pause

step "3. Two API keys: team-a may use external models, team-b may not"
RUN=$(date +%H%M%S)
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
KEY_A=$(create_key "team-a-$RUN" true)
KEY_B=$(create_key "team-b-$RUN" false)
echo "team-a-$RUN sees: $(models "$KEY_A")"
echo "team-b-$RUN sees: $(models "$KEY_B")"
pause

step "4. team-a: local model, streamed local model, external model"
chat "$KEY_A" qwen3
stream "$KEY_A" qwen3
if [[ -n ${OPENAI_API_KEY:-} ]]; then
  chat "$KEY_A" gpt-4.1-mini
else
  note "(external model skipped: OPENAI_API_KEY is not set)"
fi
pause

if [[ -n ${OPENAI_API_KEY:-} ]]; then
  step "5. team-b asks for the external model"
  chat "$KEY_B" gpt-4.1-mini
  pause
fi

step "6. The local model server goes down"
kubectl scale "deploy/$RELEASE-$LOCAL_BACKEND" --replicas=0 >/dev/null
kubectl wait --for=delete pod --timeout=2m \
  -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=$LOCAL_BACKEND" \
  >/dev/null 2>&1 || true
note "Scaled $RELEASE-$LOCAL_BACKEND to 0 replicas."
echo "team-a asks for qwen3 (falls back to the cloud if allowed and configured):"
chat "$KEY_A" qwen3
echo "team-b asks for qwen3 (never falls back to the cloud):"
chat "$KEY_B" qwen3

kubectl scale "deploy/$RELEASE-$LOCAL_BACKEND" --replicas=1 >/dev/null
note "Scaled $RELEASE-$LOCAL_BACKEND back to 1 replica; it restarts in the background."
pause

step "7. Usage report for this run (GET /admin/usage)"
usage_table key
echo
usage_table backend
pause

step "8. Metrics in Prometheus"
# Wait until Prometheus has discovered all (restarted) gateway replicas, then for
# one more scrape, so it has seen the counters of every request in this run.
replicas=$(kubectl get deploy "$RELEASE-gateway" -o jsonpath='{.spec.replicas}')
for _ in $(seq 60); do
  up=$(curl -s "$PROM/api/v1/query" --data-urlencode 'query=count(up{job="gateway"} == 1)' |
    jq -r '.data.result[0].value[1] // 0' 2>/dev/null || echo 0)
  [[ $up -ge $replicas ]] && break
  sleep 2
done
note "Waiting for the next scrape..."
sleep 12
echo "Requests by backend and outcome, summed over the $replicas gateway replicas:"
promql 'sum by (backend, status) (gateway_requests_total)'
echo "Failed attempts that triggered a fallback (to_backend=none: nothing answered):"
promql 'sum by (from_backend, to_backend) (gateway_fallbacks_total)'
echo "Requests denied by the external-access policy:"
promql 'sum by (model) (gateway_policy_denials_total)'
if [[ $LOCAL_BACKEND == vllm ]]; then
  echo "Scrape targets that are up, per job (vLLM is still restarting after step 6):"
else
  echo "Scrape targets that are up, per job:"
fi
promql 'sum by (job) (up)'

step "Done"
echo "To keep exploring:"
echo "  kubectl port-forward svc/$RELEASE-gateway $PORT:8080"
echo "  kubectl port-forward svc/$RELEASE-prometheus $PROM_PORT:9090   # then open $PROM"
