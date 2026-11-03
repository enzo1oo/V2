#!/usr/bin/env bash
# Create or update every Cloud Scheduler job (DESIGN section 7), all in Asia/Hong_Kong time, each
# calling the service with an OIDC token of the scheduler account (audience = SERVICE_URL).
#   deploy/setup_scheduler.sh
set -euo pipefail
source "$(dirname "$0")/config.sh"
require_project

env_file="$(mktemp)"
trap 'rm -f "$env_file"' EXIT
gcloud secrets versions access latest --secret "$ENV_SECRET" --project "$PROJECT" >"$env_file"
url="$(yaml_value SERVICE_URL "$env_file")"
if [ -z "$url" ]; then
  echo "SERVICE_URL is empty in $ENV_SECRET (deploy/env.prod.yaml)" >&2
  exit 1
fi

gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --member "serviceAccount:$SCHEDULER_SA" --role roles/run.invoker --quiet >/dev/null

# name | schedule | path. "*/5 8-19" runs 08:00-19:55. The WhatsApp reminder job runs every
# 30 minutes on weekdays and the app sends once, at the first run after Config "Reminder time".
jobs=(
  "grc-gmail|*/10 * * * *|/jobs/gmail"
  "grc-changes|*/5 8-19 * * *|/jobs/changes"
  "grc-recurring|0 7 * * *|/jobs/recurring"
  "grc-digest|0 9 * * 1-5|/jobs/digest"
  "grc-wa-reminders|*/30 7-19 * * 1-5|/jobs/wa-reminders"
  "grc-wa-drafts|*/10 * * * *|/jobs/wa-drafts"
)
for job in "${jobs[@]}"; do
  IFS='|' read -r name schedule path <<<"$job"
  args=(
    --project "$PROJECT" --location "$REGION"
    --schedule "$schedule" --time-zone "Asia/Hong_Kong"
    --uri "$url$path" --http-method POST
    --oidc-service-account-email "$SCHEDULER_SA" --oidc-token-audience "$url"
    --attempt-deadline 300s --quiet
  )
  if gcloud scheduler jobs describe "$name" --project "$PROJECT" --location "$REGION" \
    >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$name" "${args[@]}" >/dev/null
    echo "updated $name ($schedule)"
  else
    gcloud scheduler jobs create http "$name" "${args[@]}" >/dev/null
    echo "created $name ($schedule)"
  fi
done
