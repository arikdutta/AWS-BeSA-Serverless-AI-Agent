#!/usr/bin/env bash
# Simulates a human reviewer's decision for a booking escalated in the
# choreography pattern. Usage: send-human-decision.sh <bookingID> <approved|rejected>
set -euo pipefail

STACK_NAME="${STACK_NAME:?Set STACK_NAME to your deployed stack name}"
AWS_REGION="${AWS_REGION:?Set AWS_REGION, e.g. us-west-2}"
BOOKING_ID="${1:?Usage: send-human-decision.sh <bookingID> <approved|rejected>}"
DECISION="${2:?Usage: send-human-decision.sh <bookingID> <approved|rejected>}"

EVENT_BUS_NAME=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='ChoreographyEventBusName'].OutputValue" --output text)

TMP_ENTRIES=$(mktemp)
trap 'rm -f "$TMP_ENTRIES"' EXIT

python3 - "$BOOKING_ID" "$DECISION" "$EVENT_BUS_NAME" > "$TMP_ENTRIES" <<'PY'
import json, sys
from datetime import datetime, timezone
booking_id, decision, bus_name = sys.argv[1], sys.argv[2], sys.argv[3]
detail = {
    "bookingID": booking_id,
    "decision": decision,
    "reviewer": "tutorial-user",
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "notes": f"Manually {decision} via send-human-decision.sh",
}
print(json.dumps([{
    "Source": "workshop.human-review",
    "DetailType": "HumanApprovalDecision",
    "Detail": json.dumps(detail),
    "EventBusName": bus_name,
}]))
PY

aws events put-events --region "$AWS_REGION" --entries "file://$TMP_ENTRIES"
echo "✅ Sent HumanApprovalDecision ($DECISION) for bookingID=$BOOKING_ID"
