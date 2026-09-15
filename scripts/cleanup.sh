#!/usr/bin/env bash
# Tears down everything this tutorial created: EventBridge rules/targets, the
# human-review SQS queue, the hotel-agent stack (if deployed), and the main
# SAM stack. Run from the project root.
set -euo pipefail

STACK_NAME="${STACK_NAME:?Set STACK_NAME to your deployed stack name}"
AWS_REGION="${AWS_REGION:?Set AWS_REGION, e.g. us-west-2}"
HOTEL_STACK_NAME="${HOTEL_STACK_NAME:-${STACK_NAME}-hotel-agent}"

EVENT_BUS_NAME=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='ChoreographyEventBusName'].OutputValue" --output text 2>/dev/null || true)

if [ -n "${EVENT_BUS_NAME:-}" ] && [ "$EVENT_BUS_NAME" != "None" ]; then
  echo "Removing EventBridge rules on $EVENT_BUS_NAME..."
  for rule in InitialTravelRequestRule PlannerDatesRule WeatherCompletedRule FlightCompletedRule \
              HumanReviewRule HumanApprovalRule CatchAllEventsRule HotelAgentRule; do
    TARGET_IDS=$(aws events list-targets-by-rule --region "$AWS_REGION" --event-bus-name "$EVENT_BUS_NAME" \
      --rule "$rule" --query 'Targets[].Id' --output text 2>/dev/null || true)
    if [ -n "$TARGET_IDS" ]; then
      aws events remove-targets --region "$AWS_REGION" --event-bus-name "$EVENT_BUS_NAME" \
        --rule "$rule" --ids $TARGET_IDS >/dev/null 2>&1 || true
    fi
    aws events delete-rule --region "$AWS_REGION" --event-bus-name "$EVENT_BUS_NAME" \
      --name "$rule" >/dev/null 2>&1 || true
  done
fi

echo "Deleting human-review SQS queue..."
QUEUE_URL=$(aws sqs get-queue-url --region "$AWS_REGION" --queue-name multi-agent-human-review \
  --query QueueUrl --output text 2>/dev/null || true)
[ -n "${QUEUE_URL:-}" ] && [ "$QUEUE_URL" != "None" ] && \
  aws sqs delete-queue --region "$AWS_REGION" --queue-url "$QUEUE_URL" >/dev/null 2>&1 || true

if aws cloudformation describe-stacks --stack-name "$HOTEL_STACK_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  SNS_TOPIC_ARN=$(aws sns list-topics --region "$AWS_REGION" \
    --query "Topics[?ends_with(TopicArn, ':hotel-recommendations')].TopicArn" --output text)
  echo "Deleting hotel agent stack..."
  aws cloudformation delete-stack --region "$AWS_REGION" --stack-name "$HOTEL_STACK_NAME"
  aws cloudformation wait stack-delete-complete --region "$AWS_REGION" --stack-name "$HOTEL_STACK_NAME" || true
  if [ -n "${SNS_TOPIC_ARN:-}" ] && [ "$SNS_TOPIC_ARN" != "None" ]; then
    echo "Deleting SNS topic $SNS_TOPIC_ARN..."
    aws sns delete-topic --region "$AWS_REGION" --topic-arn "$SNS_TOPIC_ARN" || true
  fi
fi

echo "Emptying session bucket (S3 requires an empty bucket before stack deletion)..."
SESSION_BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='SessionBucketName'].OutputValue" --output text 2>/dev/null || true)
[ -n "${SESSION_BUCKET:-}" ] && [ "$SESSION_BUCKET" != "None" ] && \
  aws s3 rm "s3://$SESSION_BUCKET" --recursive --region "$AWS_REGION" >/dev/null 2>&1 || true

echo "Deleting main stack $STACK_NAME..."
aws cloudformation delete-stack --region "$AWS_REGION" --stack-name "$STACK_NAME"
aws cloudformation wait stack-delete-complete --region "$AWS_REGION" --stack-name "$STACK_NAME"

echo "✅ Cleanup complete."
