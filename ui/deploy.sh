#!/bin/bash
# ============================================================
# qast.nuts.services → Google Cloud Run deployment
# Run from the ui/ directory containing:
#   site/, docs/, admin/, Dockerfile, nginx.conf
# ============================================================
# Prerequisites: gcloud CLI installed + authenticated
#   brew install google-cloud-sdk   (or equivalent)
#   gcloud auth login
# ============================================================

set -e

PROJECT_ID="gnosis-459403"
REGION="us-central1"
SERVICE="qast-site"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}"

echo "==> 1. Setting project"
gcloud config set project $PROJECT_ID

echo "==> 2. Enabling required APIs (one-time)"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

echo "==> 3. Building + pushing container via Cloud Build"
gcloud builds submit --tag $IMAGE .

echo "==> 4. Deploying to Cloud Run"
gcloud run deploy $SERVICE \
  --image $IMAGE \
  --region $REGION \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 128Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 3

echo ""
echo "============================================================"
echo "DONE."
echo ""
echo "Cloud Run will print the service URL (*.run.app)."
echo "Use that to verify, then map qast.nuts.services when ready."
echo ""
echo "To map a custom domain later:"
echo "  gcloud run domain-mappings create \\"
echo "    --service $SERVICE \\"
echo "    --domain qast.nuts.services \\"
echo "    --region $REGION"
echo ""
echo "DNS: CNAME qast.nuts.services → ghs.googlehosted.com."
echo "SSL cert is automatic. Provisioning takes 15-30 min."
echo "============================================================"
