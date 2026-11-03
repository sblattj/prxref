# The review gate on AWS Lambda

The same gate as `prxref gate serve` ([docs/gate.md](../../docs/gate.md)), run per event on AWS
instead of as one process on a laptop. Design: [the AWS design](../../docs/design/2026-10-21-gate-on-aws.md).

```
GitHub -> HTTP API POST /webhook -> receiver (10 s) -> SQS FIFO (group repo#pr) -> worker (15 min, 1 at a time)
                                         |                    ^                        |
                                         v                    |                        v
                                     DynamoDB  <---------- watchdog (every minute)   Bedrock (execution role)
```

All three functions share one container image (`Dockerfile`, Python 3.12 Lambda base,
`prxref[aws]`); each picks its handler with `ImageConfig.Command`. litellm alone is past the
250 MB zip limit, so the worker must be an image, and one image means one build and one copy of
the code. The cost is a receiver cold start of a second or two, well inside GitHub's 10 s.

## Prerequisites

1. **AWS CLI and SAM CLI**, credentials for the target account, and a local Docker engine with
   buildx (OrbStack works) for `deploy.sh`.
2. **Bedrock model access** for the model in `ModelId` (default
   `bedrock/us.anthropic.claude-opus-4-6-v1`, a US cross-region inference profile) in the stack's
   region. Check it before deploying:
   `aws bedrock-runtime converse --model-id us.anthropic.claude-opus-4-6-v1 --messages '[{"role":"user","content":[{"text":"ping"}]}]'`.
   The worker role may call the `us.anthropic.*` inference profiles and the `anthropic.*`
   foundation models in every region the profile routes to; nothing else.
3. **The GitHub App** registered and installed as in [docs/gate.md](../../docs/gate.md)
   (Prerequisites 4 and 5), with its app id, installation id, PEM key and webhook secret.
4. **Two secrets**, created from files so no secret value is ever on a command line:

   ```bash
   aws secretsmanager create-secret --name prxref-gate/app-private-key \
     --secret-string file://path/to/app.pem
   aws secretsmanager create-secret --name prxref-gate/webhook-secret \
     --secret-string file://path/to/webhook-secret.txt
   ```

   The webhook secret file holds the secret alone, with no trailing newline
   (write it with `printf '%s'`, not `echo`). Note both ARNs. Delete the local
   copies you no longer need.

## Deploy

The stack must be named `prxref-gate`: the deploy identity below scopes every permission to
resources CloudFormation names after it, and the function boundary only allows secrets
under `prxref-gate/`.

### Once, as an admin: the deploy identity

`deployer.yaml` creates:

- `prxref-gate-cfn-exec`, the role CloudFormation acts as. It can create and change only
  `prxref-gate-*` functions, roles, queues, the table, log groups and the schedule rule, plus
  HTTP APIs in the region (their ids are random, so that one is region-wide).
- `prxref-gate-function-boundary`, a permissions boundary. The exec role can create a role
  only if it carries this boundary, so a template deployed through it cannot mint a function
  role that does more than write logs, use the gate's table and queues, read
  `prxref-gate/*` secrets and invoke Anthropic models on Bedrock.
- `prxref-gate-deployer`, the role `sam deploy` runs as: change sets on the `prxref-gate`
  stack only when they name the exec role, pass that role, push to the ECR repository, write
  the artifact bucket and read the gate's logs.
- An ECR repository `prxref-gate` (Lambda may pull only for `prxref-gate-*` functions) and an
  artifact bucket.
- An IAM user `prxref-deployer` (optional; see below to drop it once CI deploys) that can only
  assume the deployer role and use `aws login`
  (`signin:AuthorizeOAuth2Access`, `signin:CreateOAuth2Token`; without them the sign-in page
  answers "Invalid request"). It has no access keys; give it a console password (from a file,
  never a command line).

```bash
aws cloudformation deploy --stack-name prxref-gate-deployer \
  --template-file deploy/aws/deployer.yaml --capabilities CAPABILITY_NAMED_IAM
aws cloudformation describe-stacks --stack-name prxref-gate-deployer \
  --query 'Stacks[0].Outputs' --output table
```

Then two profiles in `~/.aws/config`:

```ini
[profile prxref-deployer-login]
region = us-west-2

[profile prxref-deploy]
region = us-west-2
role_arn = <DeployerRoleArn>
source_profile = prxref-deployer-login
```

`aws login --profile prxref-deployer-login`, choose IAM user, then
`aws sts get-caller-identity --profile prxref-deploy` should print the deployer role.

### Every deploy, as the deployer

The image is not built by `sam build`. Under the containerd image store (OrbStack, Docker
Desktop) `sam build` pushes a manifest that mixes Docker and OCI layer types, and Lambda fails
that image at init with `Runtime.InvalidEntrypoint` and no other output. `deploy.sh` builds with
buildx and Docker v2 media types, pushes `gate-<commit>`, and passes the digest as `ImageUri`.

The first deploy saves everything but the image to `samconfig.toml`:

```bash
cd deploy/aws
sam validate --lint
sam deploy --profile prxref-deploy --stack-name prxref-gate --template-file template.yaml \
  --role-arn <ExecRoleArn> --s3-bucket <ArtifactBucketName> \
  --image-repository <ImageRepositoryUri> --capabilities CAPABILITY_IAM \
  --parameter-overrides PermissionsBoundaryArn=<FunctionBoundaryArn> AppId=... \
    InstallationIds=... Repos=... AppKeySecretArn=... WebhookSecretArn=... \
    ReviewSkipGlobs=... ImageUri=<any pushed digest> --save-params
```

From then on every deploy, image included, is one command from the repository root:

```bash
deploy/aws/deploy.sh --profile prxref-deploy
```

It reads `image_repository` and the overrides from `samconfig.toml`, because `sam deploy
--parameter-overrides` on the command line replaces the saved set rather than adding to it.
Do not use `--resolve-s3` or `--resolve-image-repos`: they create SAM's own managed stacks,
which the deployer role is not allowed to touch. `samconfig.toml` holds ARNs with your account
id and is git-ignored, as is `.aws-sam/`. The stack outputs `WebhookUrl`, `QueueUrl`,
`DeadLetterQueueUrl` and `TableName`. The table is retained if the stack is deleted. Delete
the stack with `--role-arn <ExecRoleArn>` too.

Deploying as an admin instead also works: `sam deploy --guided` with `PermissionsBoundaryArn`
left empty.

### From GitHub Actions, with no stored keys

`.github/workflows/deploy-gate.yml` runs `deploy.sh --no-profile` on every push to main that
touches the gate. The job asks GitHub for an OIDC token, and
`aws-actions/configure-aws-credentials` trades it for the deployer role's temporary
credentials. AWS checks the token's `sub` against `GitHubDeploySubject` with `StringEquals`,
so only that repository's main branch can deploy: other branches, forks and pull requests are
refused at `sts:AssumeRoleWithWebIdentity`.

Repositories created after 2026-07-15 (and renamed, transferred or opted-in ones) send an
immutable `sub` that carries the owner and repository ids. A trust policy written as
`repo:OWNER/REPO:*` fails for them with "Not authorized to perform
sts:AssumeRoleWithWebIdentity". Read the prefix first:

```bash
gh api repos/OWNER/REPO/actions/oidc/customization/sub --jq .sub_claim_prefix
```

Then, once, as an admin, update the deploy identity with the full subject:

```bash
aws cloudformation deploy --stack-name prxref-gate-deployer --template-file deploy/aws/deployer.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "GitHubDeploySubject=<prefix>:ref:refs/heads/main"
```

Pass `GitHubOidcProviderArn=<arn>` as well when the account already has a
`token.actions.githubusercontent.com` provider (there can be only one). Then set two
repository secrets: `PRXREF_GATE_DEPLOY_ROLE_ARN` (the `DeployerRoleArn` output) and
`PRXREF_GATE_SAMCONFIG` (the contents of your `samconfig.toml`). Neither is a credential: the
role can only be assumed by the token GitHub mints for the trusted job.

Once a merge to main has deployed, the `prxref-deployer` user is no longer needed. Drop it by
passing an empty name in the same update, which also removes it from the role's trust policy
(delete its console password first, or CloudFormation cannot delete the user):

```bash
aws iam delete-login-profile --user-name prxref-deployer
aws cloudformation deploy --stack-name prxref-gate-deployer --template-file deploy/aws/deployer.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "GitHubDeploySubject=<prefix>:ref:refs/heads/main" "DeployerUserName="
```

The template refuses an update that leaves neither the user nor `GitHubDeploySubject`, since
no one could deploy. Without the user, a fix to the deploy path itself goes through an admin.

## Send a signed sample webhook

Before GitHub points at the API, sign a request yourself. The signature is computed from the
secret file by Python, so the secret never reaches argv:

```bash
URL=$(aws cloudformation describe-stacks --stack-name <stack> \
  --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text)
printf '%s' '{"zen":"ping","hook_id":1}' > ping.json
SIG=$(python3 -c 'import hmac,hashlib,sys; k=open(sys.argv[1],"rb").read(); b=open(sys.argv[2],"rb").read(); print("sha256="+hmac.new(k,b,hashlib.sha256).hexdigest())' path/to/webhook-secret.txt ping.json)
curl -s -X POST "$URL" -H "X-GitHub-Event: ping" -H "X-GitHub-Delivery: sample-1" \
  -H "X-Hub-Signature-256: $SIG" -H "Content-Type: application/json" --data-binary @ping.json
```

`{"status": "ignored", ...}` with 202 proves the route, the secret and the HMAC; a wrong secret
gives 401. To exercise the whole path, sign a `pull_request` `opened` payload for a real open PR
in an allowlisted repo (`number`, `pull_request.head.sha`, `pull_request.base.sha`/`ref`,
`repository.full_name`, `installation.id`) with `X-GitHub-Event: pull_request`, then watch the
worker's log group and the PR's `sdlc-review` check.

## Point the app at the API

GitHub → Settings → Developer settings → GitHub Apps → the app → General → Webhook URL: set it
to `WebhookUrl` and save. Stop the local gate but leave it installed:
`launchctl bootout gui/$(id -u)/dev.prxref.gate` (and the tunnel). Push to a test PR and check
the app's Advanced tab: recent deliveries should show 202.

## Operate

- **Logs:** one log group per function, 14 days.
- **Pause:** `aws dynamodb put-item --table-name <table> --item '{"pk":{"S":"META"},"sk":{"S":"PAUSED"},"paused":{"N":"1"}}'`
  (`"0"` resumes). Deliveries still queue; the worker leaves them queued and the watchdog
  resends them after resume. A paused gate leaves PRs blocked.
- **Dead letters:** a message lands in the DLQ after three failed receives (a crash, or the
  lease held by a dead invocation; redelivery waits the 90-minute visibility timeout). The row
  stays `queued` in the table, so the watchdog sends it again; the DLQ is a record, not the only
  copy.
- **Secret rotation:** put the new value with `aws secretsmanager put-secret-value --secret-id <arn>
  --secret-string file://...`, change it in the app settings, then force new containers with
  `aws lambda update-function-configuration` (any environment change) or wait for them to recycle.
  There is no previous-secret grace on Lambda; redeliver any delivery that failed in between
  from the app's Advanced tab.

## Rollback

1. Point the app's webhook URL back at the tunnel (`https://<webhook-host>/webhook`) and restart
   the local gate: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.prxref.gate.plist`
   and the tunnel plist. Generations queued on AWS are not carried over; the local watchdog's
   reconcile picks up any open PR whose head it has not reviewed.
2. To turn the AWS gate off entirely: remove the required `sdlc-review` check from the ruleset
   first (docs/gate.md, Rollback), then disable the worker's SQS trigger
   (`aws lambda update-event-source-mapping --uuid <id> --no-enabled`) and the webhook, then
   `sam delete`. The deployer role has no `lambda:*` actions, so run the trigger step with an
   administrator profile. The table is retained; delete it by hand when you no longer want the history.
