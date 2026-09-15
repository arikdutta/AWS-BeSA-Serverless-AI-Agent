#!/usr/bin/env bash
# Publishes a test travel request and shows you where to watch it flow
# through the choreography. Pass a booking ID as $1, or one is generated.
set -euo pipefail

STACK_NAME="${STACK_NAME:?Set STACK_NAME to your deployed stack name}"
AWS_REGION="${AWS_REGION:?Set AWS_REGION, e.g. us-west-2}"
BOOKING_ID="${1:-choreo-test-$(date +%s)}"

EVENT_BUS_NAME=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='ChoreographyEventBusName'].OutputValue" --output text)
PLANNER_ARN=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='PlannerFunctionArn'].OutputValue" --output text)
LOG_GROUP_NAME=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='CatchAllLogGroupName'].OutputValue" --output text)
PLANNER_FN_NAME="${PLANNER_ARN##*:function:}"

TMP_ENTRIES=$(mktemp)
trap 'rm -f "$TMP_ENTRIES"' EXIT

python3 - "$BOOKING_ID" "$EVENT_BUS_NAME" > "$TMP_ENTRIES" <<'PY'
import json, sys
booking_id, bus_name = sys.argv[1], sys.argv[2]
# Miami in September falls in Atlantic hurricane season (see weather_agent/app.py),
# and $700 is tight for 2 travelers, so this is designed to land in human review.
detail = {
    "bookingID": booking_id,
    "userId": "tutorial-user",
    "origin": "LAX",
    "destination": "Miami",
    "travel_dates": {"start": "2026-09-20", "end": "2026-09-23"},
    "travelers": 2,
    "budget": 700,
    "airline_preference": "American",
    "interests": ["beaches", "nightlife"],
}
print(json.dumps([{
    "Source": "workshop.travel-request",
    "DetailType": "TravelRequestSubmitted",
    "Detail": json.dumps(detail),
    "EventBusName": bus_name,
}]))
PY

aws events put-events --region "$AWS_REGION" --entries "file://$TMP_ENTRIES"

echo "✅ Published TravelRequestSubmitted for bookingID=$BOOKING_ID"
echo
echo "Watch it flow through:"
echo "  aws logs tail $LOG_GROUP_NAME --follow --region $AWS_REGION"
echo "  aws logs tail /aws/lambda/$PLANNER_FN_NAME --follow --region $AWS_REGION"
echo
echo "If it escalates to human review, check the queue and approve/reject with:"
echo "  aws sqs receive-message --region $AWS_REGION --queue-url \$(aws sqs get-queue-url --region $AWS_REGION --queue-name multi-agent-human-review --query QueueUrl --output text)"
echo "  ./scripts/send-human-decision.sh $BOOKING_ID approved"
