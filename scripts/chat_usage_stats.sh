#!/usr/bin/env bash
# Reports chat usage stats from the chat-backend logs exported to BigQuery
# (genetics_chat_logs.stdout, populated by the chat-backend-to-bigquery sink).
#
# Each "Streaming Anthropic [secret] chat" log line = one chat request
# (one POST /chat/v1/chat = one user turn/message). The logs carry no
# conversation/session id, so CONVERSATIONS are approximated by sessionizing
# each user's turns on an inactivity gap (GAP_MINUTES): a new conversation
# starts at the first turn or whenever the gap from the previous turn exceeds
# the threshold. Only the Anthropic provider path logs the user and the
# secret/normal flag, so OpenAI-provider chats are not counted.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project)}"
DATASET_ID="${DATASET_ID:-genetics_chat_logs}"
SINCE="${SINCE:-2026-04-01}"
GAP_MINUTES="${GAP_MINUTES:-30}"

read -r -d '' QUERY <<SQL || true
WITH turns AS (
  SELECT
    timestamp AS ts,
    REGEXP_EXTRACT(jsonPayload.message, r'\[user=([^\]]+)\]') AS user_email,
    -- session id is present only from the session-id-in-logs change onward;
    -- 'unknown' / NULL for older rows and any client that omits it
    REGEXP_EXTRACT(jsonPayload.message, r'\[session=([^\]]+)\]') AS session_id,
    jsonPayload.message LIKE '%Streaming Anthropic secret chat with model%' AS is_secret
  FROM \`${PROJECT_ID}.${DATASET_ID}.stdout\`
  WHERE timestamp >= TIMESTAMP('${SINCE}')
    AND jsonPayload.message LIKE '%Streaming Anthropic%chat with model%'
),
marked AS (
  SELECT
    user_email, is_secret, session_id,
    TIMESTAMP_DIFF(
      ts,
      LAG(ts) OVER (PARTITION BY user_email, is_secret ORDER BY ts),
      MINUTE
    ) AS gap_min
  FROM turns
),
convo AS (
  SELECT
    user_email, is_secret, session_id,
    -- new conversation at first turn (gap_min NULL) or after an idle gap
    IF(gap_min IS NULL OR gap_min > ${GAP_MINUTES}, 1, 0) AS is_new_convo,
    -- a real logged conversation id (exclude pre-change / missing ids)
    IF(session_id IS NOT NULL AND session_id != 'unknown', session_id, NULL) AS real_session
  FROM marked
)
SELECT
  COUNTIF(NOT is_secret)                                AS normal_messages,
  COUNTIF(is_secret)                                    AS secret_messages,
  SUM(IF(NOT is_secret, is_new_convo, 0))               AS normal_conv_approx,
  SUM(IF(is_secret, is_new_convo, 0))                   AS secret_conv_approx,
  COUNT(DISTINCT IF(NOT is_secret, real_session, NULL)) AS normal_conv_exact,
  COUNT(DISTINCT IF(is_secret, real_session, NULL))     AS secret_conv_exact,
  COUNT(DISTINCT IF(NOT is_secret, user_email, NULL))   AS normal_users,
  COUNT(DISTINCT IF(is_secret, user_email, NULL))       AS secret_users
FROM convo
SQL

echo "Chat usage since ${SINCE} (project=${PROJECT_ID})"
echo "conv~approx: ${GAP_MINUTES}-min inactivity-gap heuristic (covers full window)"
echo "conv~exact : distinct logged session ids (only populated from the session-id-in-logs deploy onward)"
echo

# CSV row order: normal_messages, secret_messages, normal_conv_approx,
#   secret_conv_approx, normal_conv_exact, secret_conv_exact, normal_users, secret_users
bq query --project_id="${PROJECT_ID}" --use_legacy_sql=false --format=csv "${QUERY}" \
  | awk -F, 'NR==2 {
      printf "  %-8s %10s %14s %13s %8s\n", "", "messages", "conv~approx", "conv~exact", "users"
      printf "  %-8s %10d %14d %13d %8d\n", "Normal", $1, $3, $5, $7
      printf "  %-8s %10d %14d %13d %8d\n", "Secret", $2, $4, $6, $8
    }'
