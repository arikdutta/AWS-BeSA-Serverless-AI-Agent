# Building Agentic AI Architectures with AWS Serverless — Run It On Your Own Account

This is a self-hosted recreation of AWS's [*Building Agentic AI architectures
with AWS Serverless*](https://catalog.us-east-1.prod.workshops.aws/workshops/c7bf3911-1200-4569-aafe-fbf494dd2cd6)
workshop, adapted to deploy on a personal or organizational AWS account.

## Why this exists

The official workshop's own "Initial Setup" page says, in bold, on the first
screen:

> **This workshop can only be run using AWS accounts provided at AWS-hosted
> events such as re:Invent, Immersion Days, or Summit labs. You cannot use
> your personal or organizational AWS account for this workshop.**

It hands you a temporary account with the CloudFormation stack, VSCode IDE,
and IAM policies already provisioned, tied to a 12-digit event access code.
Without that code, there's no way to follow the workshop as written — which
is presumably what you ran into.

There's also a second, sharper wall later on: the optional bonus module
(Module 3, "Add a Hotel Recommendation Agent") requires subscribing an email
address to an SNS topic and clicking a confirmation link sent by
"AWS Notifications." A corporate mail gateway that blocks external senders
will silently swallow that email, and you'll be stuck watching the CLI say
"waiting for you to confirm..." forever. That's a separate, real problem from
the account issue above, but it stacks on top of it.

This tutorial rebuilds the same architecture — three collaborating Strands
agents, EventBridge choreography, Step Functions orchestration, human-in-the-
loop review, and the optional Hotel Agent — as infrastructure-as-code you
deploy yourself. The SNS step still exists (Amazon doesn't offer a
confirmation-free alternative), but you're subscribing *your own* inbox on
*your own* account, so a work mail gateway is no longer in the loop.

**What's different from the original:** the original workshop pre-deploys the
three core agents (Planner, Weather, Flight Manager) and only shows you the
choreography/orchestration wiring — their actual source is left for you to
discover via the Lambda console *after* deployment ("the agents are
pre-built... you'll wire them together"). This tutorial includes full,
working source for all four agents, reverse-engineered from the tool names,
event schemas, and Step Functions ASL the workshop *does* publish. The
architecture, event names, and the Step Functions definition are otherwise
intentionally faithful to the original.

---

## Architecture

Three agents built with the [Strands Agents SDK](https://strandsagents.com/)
collaborate on a travel-booking decision, coordinated two different ways:

- **Planner Agent** — a cautious travel coordinator. Extracts trip details,
  aggregates weather + flight results, and decides whether to auto-approve or
  escalate to a human.
- **Weather Agent** — a meteorologist. Forecasts conditions at the
  destination and assesses travel risk.
- **Flight Manager Agent** — a booking specialist. Searches flights and
  picks the best option within budget and airline preference.

Each agent has three traits, per the workshop's framing:

| Trait | What it means here |
|---|---|
| **Personality** | A system prompt, not an if/else tree — the Planner reasons about risk instead of applying a hard budget threshold. |
| **Memory** | Each agent's Strands `Agent` has an `S3SessionManager`, giving it LLM-level conversational memory across invocations for the same booking. |
| **Tools** | Python functions exposed via `@tool` — the agent decides which to call and when, rather than following a hardcoded call sequence. |

### Pattern 1: Choreography (EventBridge)

No central controller. Agents react to events on a custom EventBridge bus,
identified by a shared `bookingID`:

```
TravelRequestSubmitted -> Planner -> DatesFinalized
                                        |
                          +-------------+-------------+
                          v                           v
                    Weather Agent               Flight Manager
                          |                           |
              WeatherAnalysisCompleted      FlightSearchCompleted
                          +-------------+-------------+
                                        v
                                    Planner
                               (waits for both)
                                        |
                    +-------------------+-------------------+
                    v                                        v
             BookingFinalized                     HumanReviewRequired -> SQS
          (+ FinalBookingCompleted)                         |
                                              HumanApprovalDecision -> Planner
                                                             |
                                                  FinalBookingCompleted
```

Because `WeatherAnalysisCompleted` and `FlightSearchCompleted` arrive as two
independent Lambda invocations, the Planner can't just hold both in memory —
it persists whichever arrives first to S3 (`state/{bookingID}/...`) and only
makes a decision once both are present. This is the "distributed state"
behavior the workshop's choreography module describes. See the docstring at
the top of [`src/planner_agent/app.py`](src/planner_agent/app.py).

Adding a capability (the optional Hotel Agent) means subscribing a new
Lambda to an event — zero changes to the three existing agents.

### Pattern 2: Orchestration (Step Functions)

A state machine explicitly sequences the same three agents:

```
PlannerExtract -> Parallel[WeatherGet, FlightSearch] -> PlannerAnalyzeAndBook
                                                              |
                                            +-----------------+-----------------+
                                            v                                   v
                                      BookingSuccess                    WaitForHuman (Activity)
                                                                                |
                                                              +-----------------+-----------------+
                                                              v                                   v
                                                   PlannerFinalizeBooking                 BookingRejected
                                                              |
                                                        BookingSuccess
```

Here the Lambda functions are *stateless* — Step Functions itself carries
`weather_data`/`flight_data` forward via `ResultPath`, so the same Planner
code just receives an `action` field (`extract` / `analyze_and_decide` /
`finalize_booking`) telling it which step it's being asked to do. See
[`statemachine/travel-booking-orchestration.asl.json`](statemachine/travel-booking-orchestration.asl.json)
for the full Amazon States Language definition (copied from the workshop,
with one real bug fixed — see "What I fixed" below). This file is a
readable reference copy — the version actually deployed is inlined directly
in `template.yaml`'s `TravelBookingStateMachine.DefinitionString`, so it's
plain `AWS::StepFunctions::StateMachine` rather than SAM's
`AWS::Serverless::StateMachine` shorthand (see "No SAM transform" below for
why).

Human review uses a **Step Functions Activity** (a pull-based task-token
pattern) instead of SQS — a different HITL mechanism than choreography uses,
which is the point of comparing the two patterns.

### Optional: Hotel Recommendation Agent

A fourth agent, wired in without touching the other three, listens for
`FinalBookingCompleted`, looks up hotels for the destination/budget, and
emails a deliberately funny recommendation via SNS. This is the module that
needs an email subscription — see [Module 3](#module-3-optional-hotel-agent--sns-email) below.

---

## What I fixed from the original

Two things surfaced while porting the workshop's published artifacts:

1. **ASL bug**: the `BookingRejected` state used `"Result"` with a
   `"rejection_reason.$": "$.humanDecision.reason"` key. In Amazon States
   Language, `.$` JSONPath interpolation is only evaluated inside
   `"Parameters"` — `"Result"` is always a literal value. As published, that
   state would emit the literal string `"$.humanDecision.reason"` under a
   key literally named `"rejection_reason.$"`, not the actual reason. Fixed
   by changing `Result` to `Parameters`.
2. **Hotel Agent event mismatch**: the bonus module's own page flags this as
   a "treasure hunt" — `FinalBookingCompleted` is sparse and the workshop
   only shows the Planner emitting it after human review, not after
   auto-approval, so the Hotel Agent only ever fires on the slow path. This
   version's Planner emits `FinalBookingCompleted` (with the full trip
   context) on **both** the auto-approve and post-human-approval paths.

## No SAM transform

Both templates in this repo (`template.yaml`, `hotel-agent.yaml`) are
deliberately written in plain CloudFormation — `AWS::Lambda::Function` and
`AWS::StepFunctions::StateMachine` instead of SAM's shorthand
`AWS::Serverless::Function` / `AWS::Serverless::StateMachine`, and no
`Transform: AWS::Serverless-2010-05-13` line at all. `sam build`/`sam
deploy` still work unchanged — SAM CLI zips and uploads a local `Code:` path
the same way it handles `CodeUri:`.

This isn't a style preference. On the account this was built against,
`sam deploy` reliably failed with:

```
User: arn:...:user/<you> is not authorized to perform:
cloudformation:CreateChangeSet on resource:
arn:aws:cloudformation:<region>:aws:transform/Serverless-2010-05-13
```

despite `AdministratorAccess` being attached, and despite adding the
commonly-published fix for this error (an explicit statement granting
`cloudformation:CreateChangeSet` on
`arn:aws:cloudformation:*:aws:transform/Serverless-*` — see
[aws/serverless-application-model#186](https://github.com/aws/serverless-application-model/issues/186)).
Both were verified live and both still failed, identically in `us-west-2`
and `us-east-1`. SCPs, RCPs, permissions boundaries, and stale credentials
were all directly ruled out. This looks like a genuine AWS-side bug specific
to this account, not a configuration mistake — see the RUNBOOK's
troubleshooting table for the full diagnostic trail. Avoiding the transform
sidesteps it entirely: a real `create-change-set` against the plain-
CloudFormation version of `template.yaml` returned `CREATE_COMPLETE` with 13
planned changes on the first try.

If your account doesn't have this problem, you can freely use the SAM
shorthand types instead — nothing about this architecture requires avoiding
them, this project just doesn't, for the reason above.

---

## Prerequisites

1. **An AWS account** you control (not a sandbox/temporary one) with billing
   enabled. Everything here runs on Lambda, EventBridge, Step Functions,
   S3, SQS, and Bedrock — all pay-per-use, and the free tier covers most of
   a single test run. Bedrock model invocations are the main real cost
   (a few cents per test run with a small model).
2. **AWS CLI v2**, configured (`aws configure` or SSO) with credentials that
   can create IAM roles, Lambda functions, EventBridge buses/rules, Step
   Functions state machines/activities, SQS queues, SNS topics, and S3
   buckets.
3. **AWS SAM CLI** (`pip install aws-sam-cli` or `brew install aws-sam-cli`).
   Plain `aws cloudformation deploy` won't install the Python dependencies
   (`strands-agents`) into each function's package — `sam build` does that
   for you.
4. **Python 3.13** locally (for `sam build`) and **Docker** (optional, for
   `sam build --use-container` if your local Python architecture doesn't
   match Lambda's).
5. **A Bedrock model your account can actually invoke** — this is the step
   the original workshop's pre-provisioned account skips for you. See below.

### Get a working Bedrock model

AWS retired the old "Model access" console page. Foundation models are now
supposed to auto-enable on first invocation — but in practice this account
hit multiple separate, independent gates when testing this project. Read
this whole section before assuming any single fix resolves it — several
looked like the fix and weren't.

1. **The critical one for this project: `Converse`/`ConverseStream` and
   `InvokeModel` are gated *differently*, even for the exact same model ID.**
   Strands (and therefore every agent in this repo) calls the model through
   the `Converse` API, because tool-calling requires it — `InvokeModel`
   doesn't support the structured tool-use loop Strands relies on. So a
   model that invokes cleanly via `InvokeModel` can still fail in the actual
   deployed Lambda. Don't trust an `invoke-model` test as proof anything is
   ready — test `converse` specifically:
   ```bash
   aws bedrock-runtime converse --region us-west-2 \
     --model-id <model-id> \
     --messages '[{"role":"user","content":[{"text":"hi"}]}]'
   ```
   On this account, `us.anthropic.claude-sonnet-4-5-20250929-v1:0` returned
   a clean response via `invoke-model` but the *identical model ID* failed
   via `converse` with the exact error in point 2 below — which is also
   exactly what showed up in the real Lambda logs once deployed. `converse`
   is the one that matters here.

2. **First-time Anthropic use requires a one-time "use case details" form,
   and it's enforced on `Converse` even when `InvokeModel` doesn't enforce
   it for the same model.** The error, from either API:
   ```
   ResourceNotFoundException: Model use case details have not been submitted
   for this account. Fill out the Anthropic use case details form before
   using the model. If you have already filled out the form, try again in
   15 minutes.
   ```
   This is genuinely the one blocker that has to be cleared by hand — it
   asks for real account/use-case details, so it's not something to script
   or fake on someone else's behalf. Fix it in the console: open the
   [Bedrock console](https://console.aws.amazon.com/bedrock/) → **Model
   catalog** → open any Anthropic model → it prompts you to submit the use
   case form the first time. This is a one-time, per-account (or per-org
   management account) step covering every Anthropic model on Bedrock.
   There's also a `PutUseCaseForModelAccess` / `aws bedrock put-use-case-
   for-model-access` API if you want to automate it later, but the console
   prompt is simpler for a first run. **Until this form is submitted, no
   model-ID swap fixes this project** — every agent needs `Converse`.

3. **Some newer/premium models are Marketplace-listed, not auto-enabled, as
   a separate concern from #2.** Testing `anthropic.claude-sonnet-5` (the
   base on-demand model ID) on this account returned:
   ```
   AccessDeniedException: anthropic.claude-sonnet-5 is not available for
   this account.
   ```
   Someone with AWS Marketplace subscription permissions has to invoke that
   specific model *once* to enable it account-wide. If you hit this, either
   get that subscription step done, or target a different model — this is
   unrelated to, and doesn't fix, #2.

4. **Newer Claude models need an inference profile ID, not the bare model
   ID, as yet another separate concern.** Calling
   `anthropic.claude-sonnet-4-5-20250929-v1:0` (the ID `list-foundation-
   models` shows) directly returns:
   ```
   ValidationException: Invocation of model ID
   anthropic.claude-sonnet-4-5-20250929-v1:0 with on-demand throughput
   isn't supported. Retry your request with the ID or ARN of an inference
   profile that contains this model.
   ```
   Prefix the model ID with the inference-profile region code —
   `us.anthropic.claude-sonnet-4-5-20250929-v1:0` — and this specific error
   goes away. It does **not**, by itself, resolve #2 or #3 — see #5, it
   turned out this specific model is Marketplace-gated too, which is why
   it's not this project's default (see #6).

5. **Marketplace subscription needs its own IAM permissions — but on this
   account, even that wasn't enough.** After clearing #2, calling `converse`
   on `us.anthropic.claude-sonnet-4-5-20250929-v1:0` returned:
   ```
   AccessDeniedException: Model access is denied due to IAM user or service
   role is not authorized to perform the required AWS Marketplace actions
   (aws-marketplace:ViewSubscriptions, aws-marketplace:Subscribe) to enable
   access to this model.
   ```
   The documented fix is granting those two actions explicitly:
   ```bash
   aws iam put-user-policy --user-name <your-user> \
     --policy-name AllowBedrockMarketplaceSubscribe \
     --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"AllowBedrockMarketplaceSubscribe","Effect":"Allow","Action":["aws-marketplace:ViewSubscriptions","aws-marketplace:Subscribe"],"Resource":"*"}]}'
   ```
   On this account that changed nothing — `aws iam simulate-principal-policy`
   confirmed both actions cleanly `allowed` (matched by both
   `AdministratorAccess` and the new statement), and the exact same
   `AccessDeniedException` still came back from a live `converse` call five
   minutes later. That points to an account-level AWS Marketplace onboarding
   gap (e.g. never having accepted Marketplace's terms or set up a
   Marketplace-specific payment method) that only the Marketplace/Bedrock
   **console** can resolve — not an IAM policy, and not fixable from the CLI.
   Rather than chase that down, this project just doesn't use a
   Marketplace-listed model — see #6.

6. **Bedrock-native models (not Marketplace-listed) skip #5 entirely.**
   `us.anthropic.claude-haiku-4-5-20251001-v1:0` invoked cleanly via
   `converse` on the first try, no use-case form drama, no Marketplace
   subscription needed. This project's `BedrockModelId` default is this
   model for exactly that reason — it's the one that's actually been
   verified end-to-end against a real account with real constraints, not
   the newest/largest model available. If you want Sonnet-quality reasoning
   and your account doesn't hit the Marketplace wall, feel free to swap it
   back at deploy time.

`list-foundation-models` shows none of this — it only tells you what's
*offered* in a region, never what your account can actually invoke:
```bash
aws bedrock list-foundation-models --region us-west-2 --by-provider anthropic \
  --query 'modelSummaries[].modelId' --output table
```

---

## Deploy

All commands assume you're in this directory, with `STACK_NAME` and
`AWS_REGION` exported (every script in `scripts/` reads these):

```bash
export STACK_NAME=agentic-travel-workshop
export AWS_REGION=us-west-2
```

### 1. Build and deploy the core stack

```bash
sam build --use-container
sam deploy \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_IAM \
  --resolve-s3 \
  --parameter-overrides BedrockModelId=us.anthropic.claude-haiku-4-5-20251001-v1:0
```

(Swap `BedrockModelId` for whatever you confirmed access to above — see
"Get a working Bedrock model" above; the value here is just this project's
default, not a guarantee it works on your account.)

This provisions: the custom EventBridge bus, an S3 session bucket, one IAM
role shared by all agent Lambdas, six Lambda functions (three for
choreography, three "Orch"-prefixed ones for orchestration — same code,
separate functions so each pattern's wiring stays independent, matching the
original workshop's two-stack split), a Step Functions execution role, a
Step Functions Activity, and the orchestration state machine (fully wired —
the Lambda ARNs are substituted into the ASL automatically via `Fn::Sub` in
`template.yaml`, replacing the original workshop's manual `sed` step).

### 2. Wire up choreography (Module 1 equivalent)

The bus and Lambdas exist, but nothing routes events between them yet —
same as the original workshop, this wiring is a separate, explicit step so
you see how choreography is actually assembled:

```bash
./scripts/wire-choreography.sh
```

This creates the EventBridge rules (`TravelRequestSubmitted` → Planner,
`DatesFinalized` → Weather + Flight, both results → Planner,
`HumanReviewRequired` → SQS, `HumanApprovalDecision` → Planner), the Lambda
invoke permissions each rule needs, the human-review SQS queue, and a
catch-all rule feeding a CloudWatch Logs group for observability.

### 3. Test choreography

```bash
./scripts/test-choreography.sh
```

This publishes a `TravelRequestSubmitted` event for a Miami trip in
September (hurricane season, per `weather_agent/app.py`) with a tight
budget — designed to land in human review, like the workshop's own
`high-risk-test-456` example. Tail the logs it prints, or open **CloudWatch
Logs Insights** on the printed log group and query:

```
fields @timestamp, @message
| parse @message /"detail-type":"(?<event_type>[^"]+)"/
| parse @message /"source":"(?<source>[^"]+)"/
| filter @message like /<YOUR_BOOKING_ID>/
| sort @timestamp asc
```

If it escalates, approve or reject it:

```bash
./scripts/send-human-decision.sh <bookingID> approved
```

Run `./scripts/test-choreography.sh some-other-id` again with a different
destination/budget in the script if you want to see the auto-approve path
instead (edit the `detail` dict in the script, or just call
`aws events put-events` directly with your own payload — see `events/` for
starting points).

### 4. Test orchestration (Module 2 equivalent)

```bash
./scripts/test-orchestration.sh
```

This starts a Step Functions execution with the same high-risk scenario
from the original workshop's Module 2 (New York → Miami, September, $800
budget), prints a console link so you can watch the execution graph, polls
for the human-review Activity task, and approves it automatically. Open the
console link to see states go green in real time — that visual graph is the
main advantage orchestration has over choreography's log-only view.

### 5. Module 3 (optional): Hotel Agent + SNS

```bash
./scripts/deploy-hotel-agent.sh
```

You'll be prompted for an email address — **use a personal inbox you can
actually open**, not a locked-down work address. This is the exact step
that fails behind a corporate mail gateway; running it against your own
account with your own inbox is what fixes it. Confirm the subscription link
AWS emails you, then the script deploys the Hotel Agent and wires
`FinalBookingCompleted` → Hotel Agent.

Re-run `./scripts/test-choreography.sh` (or approve a pending human review)
and check your inbox in a minute or two for a pun-filled hotel
recommendation email.

---

## Observability

Same tools the original workshop teaches:

- **Catch-all CloudWatch Logs group** (`/aws/events/<stack>-bus`, printed by
  `wire-choreography.sh`) — every event that crosses the bus, for
  CloudWatch Logs Insights queries by `bookingID`.
- **Per-function logs** — `aws logs tail /aws/lambda/<function-name>
  --follow` shows each agent's own reasoning trace (tool calls, decisions),
  since every `print()` in the agent code goes to CloudWatch.
- **Step Functions console** — the execution graph gives you the
  orchestration pattern's built-in visual trace for free; choreography has
  no equivalent single view, which is the actual trade-off the workshop is
  demonstrating.

---

## Cleanup

```bash
./scripts/cleanup.sh
```

Removes the EventBridge rules/targets, the human-review SQS queue, the
hotel-agent stack and its SNS topic (if deployed), empties the session S3
bucket, and deletes the main CloudFormation stack. Nothing here runs
continuously or incurs idle cost beyond the S3 bucket and CloudWatch Logs
storage, but there's no reason to leave it up once you're done.

---

## Project layout

```
template.yaml                  Main SAM template: bus, bucket, IAM role,
                                6 agent Lambdas, Step Functions role/activity/
                                state machine
hotel-agent.yaml                Optional Hotel Agent stack (deployed separately)
statemachine/
  travel-booking-orchestration.asl.json   Step Functions definition
src/
  planner_agent/app.py         Dual-mode: EventBridge choreography + Step
                                Functions "action" dispatch
  weather_agent/app.py         Same dual-mode pattern
  flight_agent/app.py          Same dual-mode pattern
  hotel_agent/app.py           Choreography-only (optional module)
scripts/
  wire-choreography.sh         Create EventBridge rules/targets/permissions + SQS
  test-choreography.sh         Publish a test TravelRequestSubmitted event
  send-human-decision.sh       Approve/reject a booking pending human review
  test-orchestration.sh        Start + drive a Step Functions execution
  deploy-hotel-agent.sh        SNS topic + subscribe + deploy + wire Hotel Agent
  cleanup.sh                   Tear everything down
```

## Known limitations (being upfront about the corners cut)

- **Dummy data everywhere** — weather and flight results come from small
  deterministic functions keyed off destination/route, not real APIs. This
  matches the original workshop's own "Workshop Simplification" callout:
  tools use dummy data to demonstrate coordination patterns, not to be a
  real travel app. Swap in real APIs (or MCP tool servers) behind the same
  `@tool` functions if you want to extend this.
- **At-least-once delivery race in choreography** — if EventBridge ever
  redelivers `WeatherAnalysisCompleted` or `FlightSearchCompleted` after the
  Planner has already made a decision and cleaned up its S3 state, it could
  in principle re-trigger a decision. This is a real characteristic of
  event-driven systems, not a bug specific to this code, and the original
  workshop doesn't address it either — worth knowing if you take this
  toward production.
- **No automated tests** — verification here is the same "run it and watch
  the logs" approach the original workshop uses.
#
