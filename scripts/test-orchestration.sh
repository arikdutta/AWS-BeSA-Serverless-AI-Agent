#!/usr/bin/env bash
# Starts a Step Functions execution with a high-risk booking, then polls the
# human-review Activity and approves it, mirroring Module 2 of the workshop.
set -euo pipefail

STACK_NAME="${STACK_NAME:?Set STACK_NAME to your deployed stack name}"
AWS_REGION="${AWS_REGION:?Set AWS_REGION, e.g. us-west-2}"
BOOKING_ID="${1:-orch-test-$(date +%s)}"

out() { aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

STATE_MACHINE_ARN=$(out StateMachineArn)
ACTIVITY_ARN=$(out HumanReviewActivityArn)

TMP_INPUT=$(mktemp)
trap 'rm -f "$TMP_INPUT"' EXIT
python3 - "$BOOKING_ID" > "$TMP_INPUT" <<'PY'
import json, sys
booking_id = sys.argv[1]
print(json.dumps({
    "bookingID": booking_id,
    "userId": "tutorial-user",
    "origin": "New York, NY",
    "destination": "Miami, FL",
    "travel_dates": {"departure": "2026-09-15", "return": "2026-09-20"},
    "travelers": {"adults": 2, "children": 0},
    "budget": 800,
    "airline_preference": "American",
    "interests": ["beach", "nightlife", "dining"],
}))
PY

EXEC_ARN=$(aws stepfunctions start-execution --region "$AWS_REGION" \
  --state-machine-arn "$STATE_MACHINE_ARN" \
  --input "file://$TMP_INPUT" \
  --name "high-risk-$BOOKING_ID" \
  --query executionArn --output text)
echo "Started execution: $EXEC_ARN"
echo "Console: https://$AWS_REGION.console.aws.amazon.com/states/home?region=$AWS_REGION#/v2/executions/details/$EXEC_ARN"

echo "Polling for the human review task (up to ~60s)..."
TASK_JSON=""
for _ in $(seq 1 6); do
  TASK_JSON=$(aws stepfunctions get-activity-task --region "$AWS_REGION" --activity-arn "$ACTIVITY_ARN" --query '{TaskToken:taskToken,Input:input}' --output json)
  if [ "$(echo "$TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("TaskToken") is not None)')" = "True" ]; then
    break
  fi
  sleep 10
done

TASK_TOKEN=$(echo "$TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("TaskToken") or "")')
if [ -z "$TASK_TOKEN" ] || [ "$TASK_TOKEN" = "None" ]; then
  echo "No task token yet - the booking may have auto-approved, or is still running. Check the console link above."
  exit 0
fi

echo "Got task token, approving booking..."
TMP_OUTPUT=$(mktemp)
trap 'rm -f "$TMP_OUTPUT"' EXIT
python3 - > "$TMP_OUTPUT" <<'PY'
import json
from datetime import datetime, timezone
print(json.dumps({
    "decision": "approved",
    "reason": "Customer accepts the risks and wants to proceed",
    "approved_by": "tutorial-user",
    "approval_timestamp": datetime.now(timezone.utc).isoformat(),
}))
PY

aws stepfunctions send-task-success --region "$AWS_REGION" \
  --task-token "$TASK_TOKEN" --task-output "file://$TMP_OUTPUT"

echo "✅ Approval sent. Check final status with:"
echo "  aws stepfunctions describe-execution --region $AWS_REGION --execution-arn $EXEC_ARN --query '{Status:status,Output:output}'"
