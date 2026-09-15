# Runbook: Agentic AI Serverless Travel Booking System

Operational procedures for deploying, testing, troubleshooting, and tearing
down this system. For architecture and design rationale, see
[README.md](README.md). This document is the "how do I actually run/fix it"
companion.

---

## When to use this runbook

- Deploying the stack to a new AWS account for the first time.
- Re-running the choreography or orchestration test flows.
- Deploying or debugging the optional Hotel Agent module.
- Something failed and you need to diagnose which agent/step broke.
- Tearing the whole thing down.

## Prerequisites and access needed

- AWS CLI v2 configured with credentials able to create IAM roles, Lambda,
  EventBridge, Step Functions, SQS, SNS, and S3 resources.
- AWS SAM CLI installed (`sam --version`).
- A Bedrock model your account can actually invoke **via `Converse`**, not
  just `InvokeModel` (see README's "Get a working Bedrock model" — the old
  "Model access" page is retired, but a fresh account can still need a
  one-time Anthropic "use case details" form and an inference-profile ID
  prefix for newer models; Marketplace-listed models add a further
  subscription gate that, on the account this was tested against, IAM
  permissions alone did not resolve. This project's default,
  `us.anthropic.claude-haiku-4-5-20251001-v1:0`, was chosen specifically
  because it's Bedrock-native and skips the Marketplace gate entirely — stick
  with it unless you've independently verified a different model via
  `converse`, not just `invoke-model`).
- `STACK_NAME` and `AWS_REGION` exported in your shell — every script here
  reads them:
  ```bash
  export STACK_NAME=agentic-travel-workshop
  export AWS_REGION=us-west-2
  ```

All commands below assume you're in this directory (`agentcore/serverless`).

---

## Procedure: first-time deployment

1. **Build.**
   ```bash
   sam build --use-container
   ```
   Use `--use-container` unless you know your local Python's platform
   matches Lambda's runtime exactly — `strands-agents` and its dependencies
   need to be installed for the Lambda architecture, not your laptop's.

2. **Deploy the core stack.**
   ```bash
   sam deploy \
     --stack-name "$STACK_NAME" \
     --region "$AWS_REGION" \
     --capabilities CAPABILITY_IAM \
     --resolve-s3 \
     --parameter-overrides BedrockModelId=us.anthropic.claude-haiku-4-5-20251001-v1:0
   ```
   (This is the model verified working end-to-end on this account — see
   README's "Get a working Bedrock model" for why. Confirm any different
   model ID you pass actually invokes for this account first —
   `list-foundation-models` only shows what's *offered* in the region, not
   what your account can *invoke* (the use-case-form and Marketplace gates
   described in the README are invisible to it). Test with a real `converse`
   call, not `invoke-model` — see README's "Get a working Bedrock model".)

   This provisions: `TravelEventBus` (EventBridge), `SessionBucket` (S3),
   `AgentExecutionRole` (shared IAM role), six Lambda functions
   (`PlannerFunction`/`WeatherFunction`/`FlightFunction` for choreography,
   `OrchPlannerFunction`/`OrchWeatherFunction`/`OrchFlightFunction` for
   orchestration — same source, separate functions), `CatchAllLogGroup`,
   `StepFunctionsExecutionRole`, `HumanReviewActivity`, and
   `TravelBookingStateMachine`. Nothing routes events yet — that's the next
   step.

3. **Wire choreography.**
   ```bash
   ./scripts/wire-choreography.sh
   ```
   Idempotent for `put-rule`/`put-targets` (safe to re-run); `add-permission`
   calls are wrapped so a rerun just logs "already exists, skipping" instead
   of failing. Creates: `InitialTravelRequestRule`, `PlannerDatesRule`,
   `WeatherCompletedRule`, `FlightCompletedRule`, `HumanReviewRule` (+ the
   `multi-agent-human-review` SQS queue), `HumanApprovalRule`,
   `CatchAllEventsRule` (feeding `CatchAllLogGroup`).

4. **Verify the deploy** before moving to test flows:
   ```bash
   aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
     --region "$AWS_REGION" --query 'Stacks[0].StackStatus'
   # expect: CREATE_COMPLETE or UPDATE_COMPLETE

   aws events list-rules --region "$AWS_REGION" \
     --event-bus-name "$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query "Stacks[0].Outputs[?OutputKey=='ChoreographyEventBusName'].OutputValue" --output text)" \
     --query 'Rules[].Name'
   # expect 7 rules from step 3
   ```

---

## Procedure: test the choreography pattern

**Known first-run issues, hit in this exact order testing this stack:**

1. Planner Lambda logs showed `ResourceNotFoundException ... Model use case
   details have not been submitted for this account`. Fix (console only —
   this form asks for real account/use-case details, not scriptable):
   Bedrock console → **Model catalog** → open any Anthropic model → submit
   the use-case form when prompted. Then **wait ~15 minutes** — no amount
   of retrying sooner helped, this is a real propagation delay. Verify it
   cleared with:
   ```bash
   aws bedrock-runtime converse --region us-west-2 \
     --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 \
     --messages '[{"role":"user","content":[{"text":"hi"}]}]'
   ```

2. After that cleared, got `AccessDeniedException ... not authorized to
   perform the required AWS Marketplace actions (aws-marketplace:
   ViewSubscriptions, aws-marketplace:Subscribe)` on a Marketplace-listed
   model. Fix:
   ```bash
   aws iam put-user-policy --user-name <your-user> \
     --policy-name AllowBedrockMarketplaceSubscribe \
     --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"AllowBedrockMarketplaceSubscribe","Effect":"Allow","Action":["aws-marketplace:ViewSubscriptions","aws-marketplace:Subscribe"],"Resource":"*"}]}'
   ```
   On the account this was tested against, that IAM fix alone did **not**
   actually resolve it (confirmed via `simulate-principal-policy` showing
   `allowed` while the live call still failed) — the working resolution was
   switching to `us.anthropic.claude-haiku-4-5-20251001-v1:0`, a
   Bedrock-native model that isn't Marketplace-listed at all, which is why
   it's this project's default. Still worth running the command above first
   if you're using a different, Marketplace-listed model — it's the
   documented fix and may well work on your account.

Both are one-time, account-level issues — once cleared (or sidestepped, for
#2), every subsequent test run (choreography, orchestration, Hotel Agent) is
unaffected. See "Get a working Bedrock model" in the README for the full
diagnostic detail on why these are separate gates that don't fix each other.

```bash
./scripts/test-choreography.sh [bookingID]
```

Publishes a `TravelRequestSubmitted` event (Miami, September, tight
budget — designed to hit human review, per `weather_agent/app.py`'s
hurricane-season logic). Prints the exact `aws logs tail` commands to watch
it.

**Expected event sequence** (watch via the printed catch-all log group or
per-function logs):

```
TravelRequestSubmitted (you published this)
  -> DatesFinalized (Planner)
       -> WeatherAnalysisCompleted (Weather Agent)
       -> FlightSearchCompleted (Flight Manager)
  -> HumanReviewRequired (Planner, once both of the above have landed)
```

If it escalates (the default test scenario will):
```bash
./scripts/send-human-decision.sh <bookingID> approved   # or: rejected
```
This publishes `HumanApprovalDecision`, which the Planner picks up and
finalizes, emitting `FinalBookingCompleted`.

**To exercise the auto-approve path instead**, publish your own event with a
low-risk destination/date and generous budget — `events/travel-request-low-
risk.json` is a ready-made example (SEA → Denver, April, $2000). It's a bare
`detail` payload; wrap it into an `entries` array the same way
`scripts/test-choreography.sh` does, or adapt that script's `DETAIL` dict
directly.

**Confirmation code note**: booking confirmations are deterministic
(`CONF-{hash(bookingID) % 1000000}`), not random — same `bookingID` always
produces the same confirmation code, which is convenient when comparing runs
or writing assertions.

---

## Procedure: test the orchestration pattern

```bash
./scripts/test-orchestration.sh [bookingID]
```

Starts a Step Functions execution with the workshop's original high-risk
scenario (New York → Miami, Sept 15–20, $800 budget — same numbers as
`events/high-risk-booking.json`), prints a console link to watch the
execution graph live, then polls `get-activity-task` for up to ~60 seconds
and auto-approves via `send-task-success` once it finds a pending task.

**If the poll times out with "No task token yet"**: the booking may have
auto-approved (unlikely with this scenario, but check the console link) or
the execution is still mid-flight. Re-run just the polling/approval half
manually:
```bash
STATE_MACHINE_ARN=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query "Stacks[0].Outputs[?OutputKey=='StateMachineArn'].OutputValue" --output text)
ACTIVITY_ARN=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query "Stacks[0].Outputs[?OutputKey=='HumanReviewActivityArn'].OutputValue" --output text)
aws stepfunctions get-activity-task --region "$AWS_REGION" --activity-arn "$ACTIVITY_ARN" --query '{TaskToken:taskToken,Input:input}'
```

**Check final status**:
```bash
aws stepfunctions describe-execution --region "$AWS_REGION" \
  --execution-arn <EXEC_ARN_from_script_output> \
  --query '{Status:status,Output:output}'
```
Expect `Status: SUCCEEDED` and an `Output` containing `booking_status:
completed` with a `booking_confirmation`.

---

## Procedure: deploy the optional Hotel Agent

```bash
./scripts/deploy-hotel-agent.sh
```

Run this **after** the core stack is deployed and `wire-choreography.sh` has
run — it reads `ChoreographyEventBusName` and `SessionBucketName` from the
main stack's outputs.

1. Creates an SNS topic (`hotel-recommendations`).
2. Prompts for an email address — **use a personal inbox you can open**, not
   a locked-down corporate one (see README's "Why this exists" for why that
   matters).
3. Blocks on `read -rp "Press Enter after you've confirmed..."` — go confirm
   the email before continuing, or the deploy proceeds with an unconfirmed
   subscription and `sns:Publish` will silently succeed but no email will
   ever arrive.
4. `sam build --template-file hotel-agent.yaml` + `sam deploy` (stack name
   defaults to `${STACK_NAME}-hotel-agent`, override via `HOTEL_STACK_NAME`).
5. Wires `HotelAgentRule` (`FinalBookingCompleted` → Hotel Agent Lambda).

**Trigger it**: `FinalBookingCompleted` is emitted by the Planner on *both*
the auto-approve and post-human-approval paths (see README's "What I fixed"
section), so either `./scripts/test-choreography.sh` with a low-risk
scenario, or `./scripts/send-human-decision.sh <id> approved` after an
escalation, will fire it.

**Verify**:
```bash
HOTEL_FUNCTION_ARN=$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}-hotel-agent" --query "Stacks[0].Outputs[?OutputKey=='HotelFunctionArn'].OutputValue" --output text)
aws logs tail /aws/lambda/${HOTEL_FUNCTION_ARN##*:function:} --follow --region "$AWS_REGION"
```
Check your inbox (and spam folder) within a minute or two.

---

## Procedure: check what this is actually costing you

Everything in this stack is pay-per-use — Lambda, EventBridge, Step
Functions, and SQS only charge when invoked, with no hourly/idle cost. The
only real cost driver is Bedrock model invocations. Two ways to check the
actual number, in order of freshness:

1. **Billing console (freshest available, still somewhat delayed)**: open
   the [Billing console](https://console.aws.amazon.com/billing/home)
   homepage — it shows a month-to-date estimated total, usually a few hours
   behind actual usage rather than a full day.

2. **Cost Explorer CLI (most detail, but ~24+ hour lag)**:
   ```bash
   aws ce get-cost-and-usage \
     --time-period Start=<YYYY-MM-DD>,End=<YYYY-MM-DD+1> \
     --granularity DAILY \
     --metrics "UnblendedCost" \
     --group-by Type=DIMENSION,Key=SERVICE \
     --region us-east-1
   ```
   Cost Explorer's `ce` API always runs in `us-east-1` regardless of where
   your resources live. **Important caveat, confirmed by testing this the
   same day resources were created**: same-day usage does not show up yet —
   querying `Start=<today>` returns whatever unrelated costs already existed
   in the account, not the Bedrock/Lambda/EventBridge usage from a stack you
   just deployed today. Query for *yesterday or earlier* to see usage from
   a session that's already a day old, and re-run the same query again
   tomorrow to see today's numbers once Cost Explorer catches up.

3. **A rough estimate without waiting on either**, based on what you can
   directly observe: since Bedrock is the only real cost, tally roughly how
   many agent invocations you triggered (each `test-choreography.sh` run is
   ~4-5 Bedrock tool-calling turns across the three-then-Planner agents; each
   `test-orchestration.sh` run is similar). At Haiku 4.5 pricing (~$1 per
   million input tokens, ~$5 per million output tokens), even a dozen full
   test runs total well under $0.10 — there's no meaningful cost risk in
   experimenting freely with the test scripts.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `sam deploy` fails: `User: ... is not authorized to perform: cloudformation:CreateChangeSet on resource: arn:aws:cloudformation:<region>:aws:transform/Serverless-2010-05-13` | **This project's templates no longer use `Transform: AWS::Serverless-2010-05-13` at all, specifically because of this.** On this account, CloudFormation's internal authorization check for invoking the transform rejected the request even under `AdministratorAccess` *and* an explicit statement granting `cloudformation:CreateChangeSet` on `arn:aws:cloudformation:*:aws:transform/Serverless-*` (the commonly-published fix, e.g. [aws/serverless-application-model#186](https://github.com/aws/serverless-application-model/issues/186)) — both tested live, both still denied, identically in `us-west-2` and `us-east-1`. SCPs/RCPs/permissions boundaries/stale credentials were all directly ruled out (`aws organizations list-policies-for-target` returned empty; `simulate-principal-policy` is unreliable for this ARN shape and shouldn't be trusted here). This looks like a genuine account-side AWS bug that needs an AWS Support case (Developer tier or above — Basic support doesn't cover technical issues) to actually root-cause. | If you're seeing this on the *current* templates, something reintroduced a `Transform:` line — remove it. `template.yaml` uses plain `AWS::Lambda::Function` (with a local `Code:` path — `sam build`/`sam deploy` still zip and upload it the same way) and `AWS::StepFunctions::StateMachine` with the ASL inlined via `DefinitionString: !Sub`, instead of the SAM shorthand types (`AWS::Serverless::Function`, `AWS::Serverless::StateMachine`) that require the transform. Verified end-to-end with a real `create-change-set` against this account: `CREATE_COMPLETE`, 13 changes, no transform involved. |
| Planner (or any agent) Lambda logs: `ResourceNotFoundException ... calling the ConverseStream operation: Model use case details have not been submitted for this account` | Strands calls the model via `Converse`/`ConverseStream` (required for tool-calling) — that API enforces the one-time Anthropic use-case form even for model IDs that invoke fine via the plain `InvokeModel` API. Testing with `aws bedrock-runtime invoke-model` looks like proof it's fixed and isn't — it tests the wrong API. Not fixed by changing `BedrockModelId` to any other model. | Bedrock console → Model catalog → open any Anthropic model → submit the use-case form when prompted; wait ~15 minutes; then verify with `aws bedrock-runtime converse --model-id <your model> --messages '[{"role":"user","content":[{"text":"hi"}]}]'` (not `invoke-model`) before retrying |
| `AccessDeniedException` from `Converse`: `Model access is denied due to IAM user or service role is not authorized to perform the required AWS Marketplace actions (aws-marketplace:ViewSubscriptions, aws-marketplace:Subscribe)`, persisting after granting those actions | **The documented IAM fix did not resolve this on this account.** Granted both actions explicitly via `put-user-policy`; `aws iam simulate-principal-policy` confirmed both cleanly `allowed` (matched by `AdministratorAccess` and the new statement); the identical `AccessDeniedException` still came back from a live `converse` call 5+ minutes later. This points to an account-level AWS Marketplace onboarding gap (e.g. Marketplace terms never accepted, no Marketplace-specific payment method) that the CLI/IAM can't see or fix — it needs the actual Marketplace/Bedrock console, not a policy change. | Don't chase this via IAM. Switch `BedrockModelId` to a Bedrock-native (non-Marketplace) model instead — `us.anthropic.claude-haiku-4-5-20251001-v1:0` invoked cleanly via `converse` on the first try with no subscription step at all, which is why it's this project's default. Verify any candidate model the same way: `aws bedrock-runtime converse --model-id <id> --messages '[{"role":"user","content":[{"text":"hi"}]}]'` |
| `AccessDeniedException ... is not available for this account` on a specific model (no mention of AWS Marketplace actions) | That model is Marketplace-listed and hasn't been subscribed/enabled account-wide yet — a product-level gate, distinct from the IAM-permission gate above, and per the row above, not reliably fixable via IAM on every account | Try the documented fix (someone with AWS Marketplace permissions invokes the model once), but don't be surprised if it doesn't take — switching `BedrockModelId` to a non-Marketplace model is the faster, verified path |
| `ValidationException: Invocation of model ID ... with on-demand throughput isn't supported. Retry your request with the ID or ARN of an inference profile` | Newer Claude models (e.g. `anthropic.claude-sonnet-4-5-...`) aren't invocable by their bare model ID at all on standard on-demand pricing — they require a cross-region inference profile ID instead. This is a routing fix, not an access fix — it does not clear any of the three gates above | Prefix the model ID with the inference profile region code, e.g. `anthropic.claude-sonnet-4-5-20250929-v1:0` → `us.anthropic.claude-sonnet-4-5-20250929-v1:0`. Then still verify with `aws bedrock-runtime converse` (not `invoke-model`) — see the rows above |
| Choreography test never progresses past `TravelRequestSubmitted` — no `DatesFinalized`, nothing in the Planner's own log group beyond an `AccessDenied` on `s3:ListBucket` | **Fixed in this repo's `template.yaml`/`hotel-agent.yaml`, but flagging in case you're on an older copy**: the agent IAM role originally granted only object-level S3 actions (`GetObject`/`PutObject`/`DeleteObject`) scoped to `bucket-arn/*`. Strands' `S3SessionManager` also calls `s3:ListBucket` to check for an existing session, which is a bucket-level action requiring the bare bucket ARN (no `/*`) — that grant was missing entirely | Confirm both `SessionState` and `SessionBucketList` statements exist in `AgentExecutionRole` (and `HotelAgentRole` in `hotel-agent.yaml`); `ListBucket`'s `Resource` must be `!GetAtt SessionBucket.Arn` with no `/*` suffix, distinct from the object-level statement. Redeploy after fixing |
| Choreography test never escalates or finalizes; only `DatesFinalized` appears in logs (Planner logs show no S3 errors) | `wire-choreography.sh` wasn't run, or ran against a different `STACK_NAME`/`EVENT_BUS_NAME` than the one currently deployed | Re-run `wire-choreography.sh` with the correct `STACK_NAME` exported; check `aws events list-rules` on the actual bus name from stack outputs |
| Planner logs show `[event] ... waiting on flight result before deciding` forever | The Flight (or Weather) Lambda errored before publishing its `*Completed` event — check its own log group, not the Planner's | `aws logs tail /aws/lambda/<stack>-FlightFunction-* --since 30m`; both agents publish an error-flagged completion event on exception (see their `except` blocks), so a total silence usually means the Lambda itself crashed or timed out (90s default) rather than raising cleanly |
| `NoSuchKey` / `ClientError` from the Planner around `_load_state`/`_save_state` | `SESSION_BUCKET` env var mismatch, or the bucket was deleted/recreated without redeploying the functions | Confirm `SessionBucketName` output matches what's in the Lambda's environment variables (`aws lambda get-function-configuration`); redeploy if they've drifted |
| Step Functions execution goes straight to `HandleError` | A Lambda invoke inside the ASL failed all retries, or `CheckPlannerDecision`'s `Default` branch was hit because `decision` wasn't exactly `"booked"` or `"needs_human_review"` | Open the execution in the Step Functions console graph, click the failed/errored state, read `error`/`cause` in its input; if it's `CheckPlannerDecision`, check `OrchPlannerFunction`'s logs for what `decision` value it actually returned |
| `send-task-success` / `test-orchestration.sh` reports no task token | Execution auto-approved (this scenario shouldn't, but double check), already timed out (`TimeoutSeconds: 3600` in the ASL), or you're polling the wrong `ACTIVITY_ARN` | Check `HumanReviewActivityArn` output matches; check execution status in the console — a `WaitForHuman` state older than 1 hour moves itself to `HumanReviewTimeout` |
| Hotel Agent never fires | `FinalBookingCompleted` rule not wired, or the booking only reached `BookingFinalized` (older code path) without emitting `FinalBookingCompleted` | Confirm `HotelAgentRule` exists (`aws events list-rules`); confirm `src/planner_agent/app.py` includes the `FinalBookingCompleted` publish on the auto-approve branch (this was a fix applied after the original build — if you've since edited that file, verify it's still there) |
| `deploy-hotel-agent.sh` fails at build: `PythonPipBuilder:Validation - Binary validation failed for python ... which did not satisfy constraints for runtime: python3.13` | `sam build` without `--use-container` requires a *local* Python interpreter matching the Lambda runtime exactly (3.13) to build the deployment package — most modern Macs have a newer local Python (e.g. 3.14) with no 3.13 anywhere on `PATH` | Already fixed in `scripts/deploy-hotel-agent.sh` (uses `--use-container` now, matching the main `sam build` in the README). If you see this on an older copy of the script, add `--use-container` to its `sam build --template-file hotel-agent.yaml` line — needs Docker running locally |
| SNS email never arrives | Subscription unconfirmed, or landed in spam, or `SNS_TOPIC_ARN` env var on the Hotel Lambda doesn't match the topic you subscribed to | `aws sns list-subscriptions-by-topic --topic-arn <arn>` — check `SubscriptionArn` isn't `PendingConfirmation`; check spam folder; confirm the Lambda's `SNS_TOPIC_ARN` env var via `get-function-configuration` |
| `sam build` fails on `strands-agents` install | No network access in the build environment, or a platform/architecture mismatch when not using `--use-container` | Re-run with `--use-container` (requires Docker running locally) |
| CloudFormation stack stuck in `ROLLBACK_COMPLETE` | A previous deploy failed partway | Must delete and redeploy — CloudFormation won't update a `ROLLBACK_COMPLETE` stack: `aws cloudformation delete-stack --stack-name "$STACK_NAME"`, wait for delete, then redeploy from step 1 |

**General diagnosis tip**: every agent's `lambda_handler` starts with
`print(f"[event] received: ...")` and logs `[agent_response]`, `[action]
emitted_event=...`, or `[error]` at each decision point — grep the
per-function CloudWatch logs for `[error]` first, then work backward through
`[agent_response]` to see what the LLM actually decided and why.

---

## Rollback

There's no in-place rollback of business logic here — this isn't a service
with live traffic, it's an on-demand demo stack. "Rollback" in practice
means one of:

- **Bad deploy (new code broke something)**: `sam deploy` again with the
  previous git commit checked out, or `aws cloudformation cancel-update-
  stack --stack-name "$STACK_NAME"` if an update is still `IN_PROGRESS`.
- **Wiring got into a bad state**: `scripts/wire-choreography.sh` is safe to
  re-run in full; it recreates every rule/target from scratch.
- **Everything's tangled and you just want a clean slate**: run the full
  teardown below, then redeploy from scratch. Given the low cost and
  ephemeral nature of this stack, this is usually faster than debugging
  partial state.

---

## Procedure: teardown

```bash
./scripts/cleanup.sh
```

Order of operations (matches dependency order — rules before the bus,
queue before nothing depends on it, hotel stack + its SNS topic before the
main stack, bucket emptied before the stack tries to delete it):

1. Removes all EventBridge rules/targets on the custom bus.
2. Deletes the `multi-agent-human-review` SQS queue.
3. If `${STACK_NAME}-hotel-agent` exists: deletes that stack, then its SNS
   topic.
4. Empties the session S3 bucket (CloudFormation can't delete a non-empty
   bucket).
5. Deletes the main stack and waits for completion.

**Partial teardown failures**: each step uses `|| true` / `2>/dev/null ||
true` for resources that may not exist, so a rerun after a partial failure
is safe. If the main stack delete itself fails (check `aws cloudformation
describe-stack-events --stack-name "$STACK_NAME" --max-items 20` for the
first `DELETE_FAILED` resource), it's almost always the S3 bucket not being
fully empty (e.g., versioned objects) — `aws s3 rm s3://<bucket> --recursive`
again, then retry `aws cloudformation delete-stack`.

---

## Escalation

This is a personal/learning project with no on-call, SLA, or production
traffic. If you're stuck:

1. Re-read the relevant agent's source in `src/*/app.py` — the business
   logic is short and the docstrings explain the choreography-vs-
   orchestration split.
2. Compare behavior against the original workshop's documented flow in
   [README.md](README.md)'s Architecture section.
3. For AWS service-level issues (not this code), the relevant service
   console (Lambda, EventBridge, Step Functions) has richer error detail
   than the CLI summaries shown above.
