#!/usr/bin/env bash
# Optional bonus module: SNS topic + email subscription, then deploys and
# wires the Hotel Recommendation Agent. Run after template.yaml is deployed
# and wire-choreography.sh has run.
#
# IMPORTANT: subscribe with a personal inbox you can actually open and click
# a confirmation link in. This is the step that fails behind a locked-down
# corporate mail gateway that blocks external senders like AWS Notifications -
# that restriction goes away once you're subscribing from your own account
# with your own inbox.
set -euo pipefail

STACK_NAME="${STACK_NAME:?Set STACK_NAME to your deployed stack name}"
AWS_REGION="${AWS_REGION:?Set AWS_REGION, e.g. us-west-2}"
HOTEL_STACK_NAME="${HOTEL_STACK_NAME:-${STACK_NAME}-hotel-agent}"

out() { aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

BUS_NAME=$(out ChoreographyEventBusName)
SESSION_BUCKET=$(out SessionBucketName)
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUS_ARN="arn:aws:events:$AWS_REGION:$AWS_ACCOUNT_ID:event-bus/$BUS_NAME"
BUCKET_ARN="arn:aws:s3:::$SESSION_BUCKET"

echo "Creating SNS topic..."
SNS_TOPIC_ARN=$(aws sns create-topic --region "$AWS_REGION" --name hotel-recommendations --query TopicArn --output text)
echo "SNS Topic: $SNS_TOPIC_ARN"

USER_EMAIL=""
while [[ "$USER_EMAIL" != *"@"*"."* ]]; do
  read -rp "Enter a personal email address to receive hotel recommendations: " USER_EMAIL
done
aws sns subscribe --region "$AWS_REGION" --topic-arn "$SNS_TOPIC_ARN" --protocol email --notification-endpoint "$USER_EMAIL"
echo "📧 Check your inbox (and spam folder) for a confirmation email from AWS Notifications and click the link."
read -rp "Press Enter after you've confirmed the subscription..."

echo "Building and deploying the Hotel Agent stack..."
sam build --use-container --template-file hotel-agent.yaml
sam deploy \
  --template-file .aws-sam/build/template.yaml \
  --stack-name "$HOTEL_STACK_NAME" \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_IAM \
  --resolve-s3 \
  --parameter-overrides \
    BusName="$BUS_NAME" BusArn="$BUS_ARN" \
    SessionBucketName="$SESSION_BUCKET" SessionBucketArn="$BUCKET_ARN" \
    SnsTopicArn="$SNS_TOPIC_ARN"

HOTEL_FUNCTION_ARN=$(aws cloudformation describe-stacks --stack-name "$HOTEL_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='HotelFunctionArn'].OutputValue" --output text)

echo "Wiring FinalBookingCompleted -> Hotel Agent..."
aws events put-rule --region "$AWS_REGION" \
  --name HotelAgentRule --event-bus-name "$BUS_NAME" \
  --event-pattern '{"source":["workshop.planner-agent"],"detail-type":["FinalBookingCompleted"]}' \
  --state ENABLED
aws events put-targets --region "$AWS_REGION" \
  --rule HotelAgentRule --event-bus-name "$BUS_NAME" \
  --targets "Id=1,Arn=$HOTEL_FUNCTION_ARN"
aws lambda add-permission --region "$AWS_REGION" \
  --function-name "$HOTEL_FUNCTION_ARN" --statement-id AllowEventBridgeHotelAgent \
  --action lambda:InvokeFunction --principal events.amazonaws.com \
  --source-arn "arn:aws:events:$AWS_REGION:$AWS_ACCOUNT_ID:rule/$BUS_NAME/HotelAgentRule" \
  >/dev/null 2>&1 || echo "  (permission already exists, skipping)"

echo "✅ Hotel Agent deployed and wired."
echo "Run a full choreography booking through to auto-approval (or approve a human-review one)"
echo "with ./scripts/test-choreography.sh and ./scripts/send-human-decision.sh to trigger it,"
echo "then watch: aws logs tail /aws/lambda/${HOTEL_FUNCTION_ARN##*:function:} --follow --region $AWS_REGION"
