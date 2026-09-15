#!/usr/bin/env bash
# Creates the EventBridge rules, targets, Lambda permissions, and the human
# review SQS queue that make the choreography pattern (Module 1) work.
# Run once after `sam deploy` for template.yaml has succeeded.
set -euo pipefail

STACK_NAME="${STACK_NAME:?Set STACK_NAME to your deployed stack name}"
AWS_REGION="${AWS_REGION:?Set AWS_REGION, e.g. us-west-2}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

out() { aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

EVENT_BUS_NAME=$(out ChoreographyEventBusName)
PLANNER_FUNCTION_ARN=$(out PlannerFunctionArn)
WEATHER_FUNCTION_ARN=$(out WeatherFunctionArn)
FLIGHT_FUNCTION_ARN=$(out FlightManagerFunctionArn)
LOG_GROUP_NAME=$(out CatchAllLogGroupName)

echo "Event Bus: $EVENT_BUS_NAME"
echo "Planner:   $PLANNER_FUNCTION_ARN"
echo "Weather:   $WEATHER_FUNCTION_ARN"
echo "Flight:    $FLIGHT_FUNCTION_ARN"

# 1. Initial travel requests -> Planner
aws events put-rule --region "$AWS_REGION" \
  --name InitialTravelRequestRule --event-bus-name "$EVENT_BUS_NAME" \
  --event-pattern '{"source":["workshop.travel-request"],"detail-type":["TravelRequestSubmitted"]}' \
  --state ENABLED
aws events put-targets --region "$AWS_REGION" \
  --rule InitialTravelRequestRule --event-bus-name "$EVENT_BUS_NAME" \
  --targets "Id=1,Arn=$PLANNER_FUNCTION_ARN"

# 2. Planner's DatesFinalized -> Weather + Flight (fan-out)
aws events put-rule --region "$AWS_REGION" \
  --name PlannerDatesRule --event-bus-name "$EVENT_BUS_NAME" \
  --event-pattern '{"source":["workshop.planner-agent"],"detail-type":["DatesFinalized"]}' \
  --state ENABLED
aws events put-targets --region "$AWS_REGION" \
  --rule PlannerDatesRule --event-bus-name "$EVENT_BUS_NAME" \
  --targets "Id=1,Arn=$WEATHER_FUNCTION_ARN" "Id=2,Arn=$FLIGHT_FUNCTION_ARN"

# 3. Weather/Flight results -> back to Planner
aws events put-rule --region "$AWS_REGION" \
  --name WeatherCompletedRule --event-bus-name "$EVENT_BUS_NAME" \
  --event-pattern '{"source":["workshop.weather-agent"],"detail-type":["WeatherAnalysisCompleted"]}' \
  --state ENABLED
aws events put-targets --region "$AWS_REGION" \
  --rule WeatherCompletedRule --event-bus-name "$EVENT_BUS_NAME" \
  --targets "Id=1,Arn=$PLANNER_FUNCTION_ARN"

aws events put-rule --region "$AWS_REGION" \
  --name FlightCompletedRule --event-bus-name "$EVENT_BUS_NAME" \
  --event-pattern '{"source":["workshop.flight-manager-agent"],"detail-type":["FlightSearchCompleted"]}' \
  --state ENABLED
aws events put-targets --region "$AWS_REGION" \
  --rule FlightCompletedRule --event-bus-name "$EVENT_BUS_NAME" \
  --targets "Id=1,Arn=$PLANNER_FUNCTION_ARN"

# 4. Lambda permissions so each rule is allowed to invoke its target
add_perm() {
  local fn="$1" sid="$2" rule="$3"
  aws lambda add-permission --region "$AWS_REGION" \
    --function-name "$fn" --statement-id "$sid" \
    --action lambda:InvokeFunction --principal events.amazonaws.com \
    --source-arn "arn:aws:events:$AWS_REGION:$AWS_ACCOUNT_ID:rule/$EVENT_BUS_NAME/$rule" \
    >/dev/null 2>&1 || echo "  (permission $sid already exists, skipping)"
}
add_perm "$PLANNER_FUNCTION_ARN" AllowEventBridgeInitialRequest InitialTravelRequestRule
add_perm "$WEATHER_FUNCTION_ARN" AllowEventBridgeDates PlannerDatesRule
add_perm "$FLIGHT_FUNCTION_ARN" AllowEventBridgeDates PlannerDatesRule
add_perm "$PLANNER_FUNCTION_ARN" AllowEventBridgeWeatherReturn WeatherCompletedRule
add_perm "$PLANNER_FUNCTION_ARN" AllowEventBridgeFlightReturn FlightCompletedRule

# 5. Human-in-the-loop: escalations go to an SQS queue for manual review
aws sqs create-queue --region "$AWS_REGION" --queue-name multi-agent-human-review >/dev/null || true
QUEUE_URL=$(aws sqs get-queue-url --region "$AWS_REGION" --queue-name multi-agent-human-review --query QueueUrl --output text)
QUEUE_ARN=$(aws sqs get-queue-attributes --region "$AWS_REGION" --queue-url "$QUEUE_URL" \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)

aws events put-rule --region "$AWS_REGION" \
  --name HumanReviewRule --event-bus-name "$EVENT_BUS_NAME" \
  --event-pattern '{"source":["workshop.planner-agent"],"detail-type":["HumanReviewRequired"]}' \
  --state ENABLED
aws events put-targets --region "$AWS_REGION" \
  --rule HumanReviewRule --event-bus-name "$EVENT_BUS_NAME" \
  --targets "Id=1,Arn=$QUEUE_ARN"
aws sqs set-queue-attributes --region "$AWS_REGION" --queue-url "$QUEUE_URL" --attributes "{
  \"Policy\": \"{\\\"Version\\\":\\\"2012-10-17\\\",\\\"Statement\\\":[{\\\"Effect\\\":\\\"Allow\\\",\\\"Principal\\\":{\\\"Service\\\":\\\"events.amazonaws.com\\\"},\\\"Action\\\":\\\"sqs:SendMessage\\\",\\\"Resource\\\":\\\"$QUEUE_ARN\\\"}]}\"
}"

# 6. Human approval decisions -> back to Planner
aws events put-rule --region "$AWS_REGION" \
  --name HumanApprovalRule --event-bus-name "$EVENT_BUS_NAME" \
  --event-pattern '{"source":["workshop.human-review"],"detail-type":["HumanApprovalDecision"]}' \
  --state ENABLED
aws events put-targets --region "$AWS_REGION" \
  --rule HumanApprovalRule --event-bus-name "$EVENT_BUS_NAME" \
  --targets "Id=1,Arn=$PLANNER_FUNCTION_ARN"
add_perm "$PLANNER_FUNCTION_ARN" AllowEventBridgeHumanApproval HumanApprovalRule

# 7. Catch-all monitoring rule -> CloudWatch Logs
aws events put-rule --region "$AWS_REGION" \
  --name CatchAllEventsRule --event-bus-name "$EVENT_BUS_NAME" \
  --event-pattern '{"source":["workshop.travel-request","workshop.planner-agent","workshop.weather-agent","workshop.flight-manager-agent","workshop.human-review","workshop.hotel-agent"]}' \
  --state ENABLED
aws events put-targets --region "$AWS_REGION" \
  --rule CatchAllEventsRule --event-bus-name "$EVENT_BUS_NAME" \
  --targets "Id=1,Arn=arn:aws:logs:$AWS_REGION:$AWS_ACCOUNT_ID:log-group:$LOG_GROUP_NAME"
aws logs put-resource-policy --region "$AWS_REGION" \
  --policy-name "${STACK_NAME}-EventBridgeLogsPolicy" \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"events.amazonaws.com\"},\"Action\":[\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"arn:aws:logs:$AWS_REGION:$AWS_ACCOUNT_ID:log-group:$LOG_GROUP_NAME:*\"}]}"

echo "✅ Choreography wired: EventBridge rules, Lambda permissions, human-review SQS queue, catch-all log group."
