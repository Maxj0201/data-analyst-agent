# Deploying to Google Cloud Run

Two Cloud Run services from one Dockerfile:

| Service              | Docker target | Port   | Access                                        |
|----------------------|---------------|--------|-----------------------------------------------|
| `analyst-agent-api`  | `api`         | `8080` | **private**, invoked only by the frontend SA  |
| `analyst-agent-ui`   | `frontend`    | `8080` | public (or behind IAP, see the end)           |

Configuration is entirely `AGENT_*` environment variables (plus `ANTHROPIC_API_KEY` and
`LANGFUSE_*`). All values come from **Secret Manager**: nothing sensitive lands in the service
spec, the image, or shell history.

## 0. Variables

```bash
export PROJECT_ID=my-project
export REGION=europe-west1
export REPO=analyst-agent
export IMAGE_BASE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO
export TAG=$(git rev-parse --short HEAD)
export API_SA=analyst-agent-api@$PROJECT_ID.iam.gserviceaccount.com
export UI_SA=analyst-agent-ui@$PROJECT_ID.iam.gserviceaccount.com
```

## 1. One-time project setup

```bash
gcloud config set project $PROJECT_ID
```

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com
```

```bash
gcloud artifacts repositories create $REPO --repository-format=docker --location=$REGION
```

```bash
gcloud iam service-accounts create analyst-agent-api --display-name="Analyst agent API"
```

```bash
gcloud iam service-accounts create analyst-agent-ui --display-name="Analyst agent UI"
```

## 2. Build and push the images

```bash
gcloud auth configure-docker $REGION-docker.pkg.dev
```

```bash
docker build --platform linux/amd64 --target api -t $IMAGE_BASE/api:$TAG .
```

```bash
docker build --platform linux/amd64 --target frontend -t $IMAGE_BASE/ui:$TAG .
```

```bash
docker push $IMAGE_BASE/api:$TAG && docker push $IMAGE_BASE/ui:$TAG
```

## 3. Put the configuration in Secret Manager

Write the API's configuration to a local file that is **never committed** (`.env*` is in
`.gitignore`/`.dockerignore`):

```bash
cat > .env.cloudrun <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...
AGENT_MODEL=claude-opus-5-5
AGENT_MAX_TURNS=15
AGENT_TOKEN_BUDGET=400000
AGENT_MAX_CONCURRENT_JOBS=4
AGENT_MAX_UPLOAD_MB=50
AGENT_LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://langfuse.example.com
EOF
```

Create (or add a new version of) one secret per variable, named after the variable, grant the
API service account read access, and collect the `--set-secrets` mapping:

```bash
SECRETS=""
while IFS='=' read -r key value; do
  [[ -z "$key" || "$key" == \#* ]] && continue
  if gcloud secrets describe "$key" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$key" --data-file=-
  else
    printf '%s' "$value" | gcloud secrets create "$key" --replication-policy=automatic --data-file=-
  fi
  gcloud secrets add-iam-policy-binding "$key" \
    --member="serviceAccount:$API_SA" --role=roles/secretmanager.secretAccessor >/dev/null
  SECRETS+="${SECRETS:+,}${key}=${key}:latest"
done < .env.cloudrun
echo "$SECRETS"
```

To change one value later, add a secret version and roll out a new revision (`:latest` is
resolved at deploy time):

```bash
printf '%s' 25 | gcloud secrets versions add AGENT_MAX_TURNS --data-file=-
```

```bash
gcloud run services update analyst-agent-api --region $REGION --update-secrets AGENT_MAX_TURNS=AGENT_MAX_TURNS:latest
```

## 4. Deploy the API

```bash
gcloud run deploy analyst-agent-api \
  --image $IMAGE_BASE/api:$TAG \
  --region $REGION \
  --service-account $API_SA \
  --no-allow-unauthenticated \
  --set-secrets "$SECRETS" \
  --set-env-vars AGENT_DATA_DIR=/app/data,AGENT_ARTIFACTS_DIR=/app/artifacts,AGENT_JOB_DB_PATH=/app/data/jobs.sqlite3 \
  --cpu 2 --memory 2Gi \
  --no-cpu-throttling \
  --min-instances 0 --max-instances 1 \
  --concurrency 40 \
  --timeout 3600
```

Why these flags:

* `--no-cpu-throttling`: jobs run in background threads *after* `POST /ask` has returned. With
  the default request-based billing, Cloud Run throttles CPU between requests and jobs stall.
* `--max-instances 1`: the job store is per instance (memory plus a local SQLite file). A
  `GET /jobs/{id}` or SSE request that lands on a different instance would 404. Raise this only
  after moving jobs to shared storage. `--session-affinity` is best-effort and not enough on its own.
* `--timeout 3600`: an SSE stream is one long request and lasts as long as the job.
* `--min-instances 0`: the instance's filesystem is in memory and lost when it scales to zero,
  and uploads, charts and job history go with it. Fine for a demo. To keep uploads and charts,
  mount a bucket (keep SQLite on the local disk; it must not live on a FUSE mount):

  ```bash
  gcloud run services update analyst-agent-api --region $REGION \
    --add-volume name=files,type=cloud-storage,bucket=$PROJECT_ID-analyst-agent \
    --add-volume-mount volume=files,mount-path=/mnt/files \
    --update-env-vars AGENT_DATA_DIR=/mnt/files/data,AGENT_ARTIFACTS_DIR=/mnt/files/artifacts,AGENT_JOB_DB_PATH=/app/data/jobs.sqlite3
  ```

Smoke test from your machine (your user needs `roles/run.invoker` on the service, or be an owner):

```bash
API_URL=$(gcloud run services describe analyst-agent-api --region $REGION --format='value(status.url)')
```

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" $API_URL/healthz
```

## 5. Deploy the frontend

Let the UI's service account invoke the private API:

```bash
gcloud run services add-iam-policy-binding analyst-agent-api --region $REGION \
  --member="serviceAccount:$UI_SA" --role=roles/run.invoker
```

```bash
gcloud run deploy analyst-agent-ui \
  --image $IMAGE_BASE/ui:$TAG \
  --region $REGION \
  --service-account $UI_SA \
  --allow-unauthenticated \
  --set-env-vars AGENT_API_URL=$API_URL,AGENT_API_ID_TOKEN=true \
  --cpu 1 --memory 1Gi \
  --session-affinity \
  --timeout 3600
```

`AGENT_API_ID_TOKEN=true` makes the UI attach a Google ID token (audience = `AGENT_API_URL`)
fetched from the metadata server. `--session-affinity` keeps a Streamlit websocket session on
one instance.

> **Cost warning:** a public UI lets anyone spend your Anthropic credits. For anything beyond a
> private demo, put the UI behind Identity-Aware Proxy, or deploy it with
> `--no-allow-unauthenticated` and reach it through `gcloud run services proxy analyst-agent-ui
> --region $REGION --port 8501`.

## 6. Updating

```bash
export TAG=$(git rev-parse --short HEAD)
```

Rebuild and push (step 2), then:

```bash
gcloud run deploy analyst-agent-api --region $REGION --image $IMAGE_BASE/api:$TAG
```

```bash
gcloud run deploy analyst-agent-ui --region $REGION --image $IMAGE_BASE/ui:$TAG
```

Logs:

```bash
gcloud run services logs read analyst-agent-api --region $REGION --limit 100
```
