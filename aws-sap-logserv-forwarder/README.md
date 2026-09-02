<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# AWS SAP LogServ Forwarder Solution

> **Important:** This solution is provided as a reference implementation. Review and adapt IAM policies, encryption settings, resource configurations, and network controls to meet your organisation's specific security, compliance, and operational requirements before deploying to a production environment.

## LogServ Overview

LogServ is an SAP ECS service for collection, storage, forwarding and access of logs. It centralizes logs from all systems, applications, and ECS services used by a RISE with SAP customer. The architecture consists of:

- **Data Landing Zone (DLZ)** — Collection and pre-processing of logs
- **Customer Landing Zone (CLZ)** — Isolated data lake per customer with:
  - Customer Gateway Server (CGS) — intermediary forwarder
  - Object Store (S3) — log storage with retention policies
  - Event Notifications (SQS) — notifies on new objects

Log format: **TCP JSON compressed (json.gz)** — gzip-compressed NDJSON files.

## Log Fowarder Solution 

This solution provides a high-throughput AWS Lambda function that processes SAP ECS LogServ log files from a cross-account Amazon Simple Queue Service (SQS) queue, decompresses gzipped Newline-Delimited JSON (NDJSON) files, applies category-based filtering, and copies qualifying files to a destination Amazon Simple Storage Service (S3) bucket.

## Architecture

```
                                                ┌───────────────────────┐  ┌───────────────────────────┐
                                                │ Log Forwarder Lambda  │  │ Destination bucket        │
┌────────────────────┐  ┌────────────────────┐  │ (Customer Account)    │  │ (Customer Account)        │
│ SAP ECS CLZ Bucket │  │ SQS Queue          │  │ - Parse S3 event      │  │ logserv/<category>/<sub>/ │
│ (SAP RISE Account) │─>│ (SAP RISE Account) │─>│ - Filter by category  │─>│ <year>/<month>/<day>/     │
│                    │  │                    │  │ - Decompress json.gz  │  │ <filename>.json           │
└────────────────────┘  └────────────────────┘  │ - Copy to destination │  │                           │
                                                └───────────────────────┘  └───────────────────────────┘
```

## Log Categories & Data Sources

### Infrastructure Logs

| Category      | Subcategories                                                    | Source |
|---------------|------------------------------------------------------------------|--------|
| linux         | messages, localmessages, warn, sudolog, cron, linux_secure, lastlog, who, slapd, pacemaker, proxy | /var/log/* |
| dns           | binddns                                                           | /var/log/named/*.log |
| windows       | WinEventLog:Application, WinEventLog:System, WinEventLog:Security | Windows Event Log |

### Application Logs

In the table below, `<SID>` refers to the SAP System Identifier (SID) — the unique three-character code assigned to each SAP instance.

| Category       | Subcategories                                                    | Source |
|---------------|------------------------------------------------------------------|--------|
| abap          | workprocess, dispatcher, transport, icm, gateway, event, sapstartsrv, messageserver, enqueueserver | /usr/sap/\<SID\>/D\<Inst\>/work |
| hana          | hanaaudit, tracelogs                                             | /var/log/hana, /usr/sap/*/HDB*/*/trace |
| java          | deploy_traces, jsmon_traces, jstart_traces, java_server_traces, icm_traces, security, traces_logs | /usr/sap/*/J*/work |
| webdispatcher | process, icm, sapstartsrv, accesslog, devicm                     | /usr/sap/\<SID\>/W\<Inst\>/work |
| sap           | saphostexec, saprouter, sapstartsrv                              | /usr/sap/* |
| scc           | audit, tracelogs                                                 | /opt/sap/scc/log |
| bobj_bi       | BI-IPS, webapp, tomcat                                           | /usr/sap/\<SID\>/SBO/sap_bobj/logging |
| sybase        | install                                                          | /sybase/\<SID\>/ASE-*/install |

### AWS Infrastructure Logs (AWSLogs)

In addition to SAP application logs, the source CLZ bucket delivers AWS infrastructure logs under the standard `AWSLogs/` prefix. These are passed through by the forwarder without category filtering and stored under `<DEST_PREFIX>/AWSLogs/` in the destination bucket.

| Log Type | S3 Path Pattern | Format |
|----------|----------------|--------|
| Virtual Private Cloud (VPC) Flow Logs | `AWSLogs/<account-id>/vpcflowlogs/<region>/<year>/<month>/<day>/` | Plain text (space-delimited) |
| Elastic Load Balancing (ELB) Access Logs | `AWSLogs/<account-id>/elasticloadbalancing/<region>/<year>/<month>/<day>/` | Plain text (space-delimited) |
| AWS Web Application Firewall (WAF) Logs | `AWSLogs/<account-id>/WAFLogs/<region>/<waf-acl-name>/<year>/<month>/<day>/<hour>/<min>/` | JSON |

These logs originate from the SAP RISE managed account and provide network, load balancer, and web application firewall visibility for the SAP landscape infrastructure.

### Filtering

Log forwarding can be controlled using two parameters:

- **`IncludeCategories`** — Allowlist mode. Only forward logs matching these categories. Leave blank to forward all categories.
  ```
  IncludeCategories: "hana,linux,abap"
  ```

- **`ExcludeSubcategories`** — Denylist mode. Skip specific subcategories regardless of their parent category.
  ```
  ExcludeSubcategories: "audit,proxy,slapd"
  ```

Filters are applied to the S3 key path structure: `logserv/<category>/<subcategory>/...`

**Note:** AWS infrastructure logs (`AWSLogs/` prefix) bypass category filtering when `ForwardAWSLogs` is `true` (default). Set to `false` to exclude them entirely.

## Deployment

### Prerequisites
- [AWS Serverless Application Model (SAM) CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) installed
- [AWS CLI credentials configured](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html) for the target account
- Cross-account SQS and S3 access already configured to allow access from the target account (request via [SAP ECS Service Request "Manage security LogServ"](https://me.sap.com/servicessupport/myservices))

#### Information Required from SAP

The following are provided by SAP when LogServ is enabled on the RISE landscape:

| Detail | Example | Description |
|--------|---------|-------------|
| Source S3 Bucket | `sap-hec-clz-123456789012-us-east-1-hec99-xyz` | The CLZ bucket where LogServ writes logs |
| SQS Queue Name | `sap-hec-clz-123456789012-us-east-1-hec99-xyz-queue` | Notification queue for new log files |

The SAP account ID is typically embedded in the bucket/queue name (e.g. `123456789012`). You'll need to provide this as the `SourceAwsAccountId` parameter. The SQS ARN is constructed automatically by the template from the queue name, account ID, and deployment region.

### Deployment

All deployment commands below are run from the solution subdirectory. After cloning the repository:

```bash
cd aws-sap-logserv-forwarder
```

#### Quick Deploy (Recommended)

Use the included deploy script which auto-detects first-time vs update deployments:

```bash
# Linux/macOS/WSL
chmod +x deploy.sh
./deploy.sh --region us-east-1 --profile my-aws-profile
```

The script automatically:
1. Detects whether the stack already exists
2. If **new stack**: runs a two-step deploy (infrastructure first, then bucket + SQS trigger)
3. If **existing stack**: runs a single update

#### Manual Deployment

##### First-time deployment (new account) — Two-step process

Fresh deployments to a new AWS account require **two sequential `sam deploy` commands**. This is a documented limitation of AWS CloudFormation's pre-deployment validation, not a limitation of this solution.

**Why two steps are required:**

Since November 2025, CloudFormation runs a pre-deployment validation hook (`AWS::EarlyValidation::ResourceExistenceCheck`) on all `CreateStack`, `UpdateStack`, and `CreateChangeSet` operations. This hook validates that resources referenced in the template (such as SQS queues in event source mappings) are accessible from the Lambda execution role *before* any resources are provisioned.

For cross-account SQS event source mappings (which this solution uses to poll the SAP RISE-owned queue), the validation fails on a brand-new stack because:

1. The Lambda execution role doesn't exist yet (it's being created by the same stack)
2. CloudFormation can't verify the role's cross-account SQS permissions
3. The validation fails and the entire stack operation is rejected

**Solution:**

```bash
sam build

# Step 1: Create IAM role, Lambda, DLQ, and alarms (no SQS trigger, no dest bucket)
sam deploy --guided --region <region> --profile <aws-profile> \
  --parameter-overrides EnableSQSTrigger=false CreateDestBucket=false

# Step 2: Add destination bucket and enable SQS trigger (role now exists)
sam deploy --region <region> --profile <aws-profile> \
  --parameter-overrides EnableSQSTrigger=true CreateDestBucket=true
```

Step 1 creates the Lambda execution role with cross-account SQS permissions. Step 2 then passes CloudFormation's validation because the role already exists and can be verified against the SQS queue.

> **Note:** This two-step process is only needed for the **first** deployment to a new account. All subsequent updates (code changes, parameter changes, scaling) are single-step deploys with the default parameters (`EnableSQSTrigger=true`, `CreateDestBucket=true`).

##### Subsequent updates

```bash
sam build
sam deploy --region <region> --profile <aws-profile>
```

Replace `<region>` with the target AWS region (e.g. `us-east-1`) and `<aws-profile>` with your configured AWS CLI profile name. These values are saved to `samconfig.toml` after the first guided deploy, so subsequent runs only need `sam deploy`.

The CloudFormation stack name can be set in `samconfig.toml` under `stack_name`. Convention is `aws-sap-logserv-forwarder-<environment>` (e.g. `aws-sap-logserv-forwarder-test`).

To use a specific config file:
```bash
sam deploy --config-file samconfig-test.toml
```

#### Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `Environment` | No | *(blank)* | Environment suffix appended to resource names (e.g. `dev`, `prod`). Leave blank for no suffix. |
| `DestBucketName` | **Yes** | | Destination S3 bucket name for processed logs. |
| `SourceBucketName` | **Yes** | | Source S3 bucket name (the CLZ staging bucket). Also acts as an allowlist: events naming a different bucket are rejected before the object is read. Additional buckets can be permitted via the `SOURCE_BUCKET_ALLOWLIST` environment variable. |
| `SourceSqsQueueName` | **Yes** | | SQS queue name (as provided by SAP). The full ARN is constructed automatically using this name, the `SourceAwsAccountId`, and the deployment region. Example: `sap-hec-clz-123456789012-us-east-1-hec45-gto-queue` |
| `SourceAwsAccountId` | **Yes** | | AWS account ID of the SAP RISE managed account that owns the source SQS queue and S3 bucket. Typically embedded in the bucket/queue name (e.g. `123456789012`). |
| `IncludeCategories` | No | all | Comma-separated log categories to include. Leave blank to forward all categories (no filtering). When specified, only listed categories are forwarded — unlisted categories are silently dropped. Options: `abap`, `dns`, `hana`, `linux`, `sap`, `scc`, `webdispatcher`. |
| `ExcludeSubcategories` | No | *(none)* | Comma-separated subcategories to exclude (e.g. `audit`, `proxy`, `slapd`). |
| `MaxFileSizeMB` | No | `50` | Maximum file size in MB to process. Enforced against the object's real size before download; files exceeding the limit are skipped with a warning. Must be less than ~400 MB due to Lambda memory constraints (512 MB allocated). Recommended: 50. |
| `DestPrefix` | No | `logserv/` | S3 key prefix for objects written to the destination bucket. |
| `Decompress` | No | `true` | Decompress `.gz` files before storing in destination. |
| `LogLevel` | No | `INFO` | Lambda logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `ForwardAWSLogs` | No | `true` | Forward AWS infrastructure logs (VPC Flow Logs, ELB Access Logs, WAF Logs) from the `AWSLogs/` prefix. Set to `false` to skip. |
| `BatchSize` | No | `10` | Number of SQS messages per Lambda invocation. Range: 1–10,000. Recommended: 10. Higher values increase per-invocation duration and memory pressure. Values above 100 risk Lambda timeouts (5 min limit) when processing large files. |
| `BatchWindow` | No | `5` | Maximum batching window in seconds. Range: 0–300. Set to 0 for immediate processing (no batching). Higher values reduce invocations but increase delivery latency. |
| `ReservedConcurrency` | No | `50` | Reserved concurrent Lambda executions. Recommended: 10–100. Setting above 500 may starve other Lambda functions in the account (default account limit is 1,000 unreserved concurrency). Setting below 5 may cause SQS message backlog during bursts. Must not exceed your account's unreserved concurrency quota. |
| `CreateDestBucket` | No | `true` | Set to `false` if the destination bucket already exists. |
| `EmitMetrics` | No | `false` | Enable CloudWatch EMF custom metrics. Adds ~$15–20/month in CloudWatch costs (see Cost Estimate section). |
| `RetentionDays` | No | `365` | S3 lifecycle expiration in days for the destination bucket. Minimum: 1. LogServ source retention is 365 days — setting shorter means logs cannot be re-forwarded from source after local expiry. |
| `AllowedPrincipalArns` | No | *(blank)* | Comma-separated IAM role/user ARNs granted read access to the destination bucket. Use specific ARNs (e.g. `arn:aws:iam::123456789012:role/MyAnalyticsRole`) for least-privilege cross-account access. |
| `EnableSQSTrigger` | No | `true` | Whether to create the SQS event source mapping. Set to `false` on **first deployment** to a new account, then immediately redeploy with `true`. See [Deployment](#deployment) for details. All subsequent updates should leave this as `true`. |
| `AlarmNotificationTopicArn` | No | *(blank)* | SNS topic ARN for CloudWatch alarm notifications (DLQ, errors, throttles). Leave blank to disable notifications. |
| `AccessLogBucketName` | No | *(blank)* | S3 bucket name for server access logging on the destination bucket. Leave blank to disable. See [Enabling S3 Access Logging](#enabling-s3-access-logging). |

### Enabling S3 Access Logging

S3 server access logging provides detailed records of requests made to the destination bucket. This is optional but recommended for production environments requiring audit trails or compliance evidence.

#### Step 1: Create or identify a logging bucket

The access log bucket must be in the same AWS region as the destination bucket. If you don't have one, create a dedicated logging bucket:

```bash
aws s3api create-bucket \
  --bucket my-logserv-access-logs \
  --region us-east-1

aws s3api put-public-access-block \
  --bucket my-logserv-access-logs \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

#### Step 2: Grant the S3 logging service write access

Add a bucket policy to the logging bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ServerAccessLogsPolicy",
      "Effect": "Allow",
      "Principal": {
        "Service": "logging.s3.amazonaws.com"
      },
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::my-logserv-access-logs/*",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "YOUR_ACCOUNT_ID"
        }
      }
    }
  ]
}
```

#### Step 3: Deploy with access logging enabled

```bash
sam deploy --parameter-overrides AccessLogBucketName=my-logserv-access-logs
```

Access logs will be written to `s3://my-logserv-access-logs/<dest-bucket-name>-access-logs/`.

> **Cost:** Access logging adds ~$0.01 per 1,000 log records delivered, plus standard S3 storage costs. For a typical LogServ deployment (~750K PUT requests/month from the forwarder plus downstream reads), expect ~$1–5/month.

## Key Design Decisions

1. **Gzip decompression** — LogServ delivers files in `json.gz` format. We decompress and store as plain `.json` for easier downstream consumption (Cloudwatch, Athena, OpenSearch, etc.).

2. **Batch processing with partial failures** — Uses `ReportBatchItemFailures` so only failed messages return to the queue, not the entire batch. Each S3 record within a message is processed in its own `try`/`except`, so a failure on one record does not abort its siblings. A message is returned for retry only if one of its records fails with a transient error; permanent failures (oversized object, corrupt gzip) are logged and skipped without returning the message.

3. **arm64 + Python 3.12** — Better price/performance than x86_64 + Python 3.9.

4. **512MB memory** — Compressed files can expand significantly. Audit logs can be 20MB+ compressed. 512MB provides headroom for decompression.

5. **Reserved concurrency = 50** — Prevents runaway scaling while handling burst loads. The SQS integration automatically scales Lambda invocations up to this limit.

6. **Adaptive retry mode** — boto3 adaptive retries handle S3 throttling gracefully.

7. **Dead-Letter Queue (DLQ) with 14-day retention** — Failed messages are preserved for investigation with SQS-managed encryption at rest. LogServ retention is 365 days, so we have time to reprocess.

8. **Amazon CloudWatch alarms** — DLQ depth, errors, and throttles are monitored. When `AlarmNotificationTopicArn` is provided, all alarms send notifications (Alarm, OK, and InsufficientData state changes) to the specified SNS topic.

9. **Streaming decompression with size limit** — Prevents decompression bomb attacks. Files are decompressed in 64KB chunks with a configurable ceiling (`MAX_DECOMPRESSED_SIZE_MB`, default 500MB). Files exceeding the limit are rejected with a `DecompressionBombError`.

10. **S3 key path validation** — All incoming S3 keys are validated against expected LogServ path patterns (`logserv/<category>/<subcategory>/...` or `AWSLogs/<account-id>/...`). Keys containing path traversal sequences (`../`), backslashes, or control characters are rejected before any S3 API call is made.

11. **Strict message parsing** — Only standard S3 event notification format (with `Records[].s3.bucket.name` and `Records[].s3.object.key`) is accepted. Non-standard message formats are logged as warnings and skipped.

12. **Least-privilege IAM** — The Lambda execution role grants only `s3:PutObject` on the destination bucket (not full CRUD). Source bucket access is read-only. SQS permissions are scoped to the specific queue ARN.

13. **Principal-level cross-account access** — The destination bucket policy accepts specific IAM role/user ARNs (`AllowedPrincipalArns`) rather than account-root principals, enforcing least-privilege for cross-account consumers.

14. **Source bucket pinning** — The bucket name in the S3 event is attacker-influenceable, so it is checked against an allowlist (`SOURCE_BUCKET_NAME` plus any entries in `SOURCE_BUCKET_ALLOWLIST`) before the object is read. Events naming a bucket outside the allowlist are rejected. When the allowlist is empty, pinning is disabled and a warning is logged at cold start; set `SOURCE_BUCKET_NAME` in production to constrain the source.

15. **Authoritative size check** — The file size limit is enforced against the object's real `ContentLength` (from `get_object`) before the body is read into memory, not against the size reported in the event. The event size is used only as an advisory pre-filter. If `ContentLength` is absent, the body is read with a hard cap (limit + 1 byte) and rejected if it exceeds the limit, so the read is never unbounded. The source stream is closed on the reject path so oversized objects cannot exhaust the connection pool. This prevents an understated event size from bypassing the limit and exhausting memory.

16. **Idempotent writes** — When `SKIP_IF_EXISTS` is set (default `true`), the upload uses a conditional PUT (`IfNoneMatch="*"`), which S3 accepts only if the key does not already exist; a pre-existing key returns `412 PreconditionFailed`, which is treated as "already written" and skipped. This is atomic, so it also holds when two invocations process the same object concurrently (SQS at-least-once redelivery), and it requires only `s3:PutObject` on the destination — no read permission. This assumes source objects are immutable under a given key; if a key is ever rewritten with new content, the destination keeps the first copy.

17. **Fail on undecompressable input** — A file that signals gzip (by extension, `Content-Encoding`, or magic bytes) but fails to decompress is treated as a permanent failure: it is logged, counted as filtered, and dropped rather than stored as-is under a `.json` name. Because the same object fails identically on every retry, it is not returned for SQS redelivery, which would otherwise re-process its healthy siblings on each attempt. This prevents corrupt or mislabelled content from entering the destination as apparent JSON.

18. **Log sanitisation** — Untrusted values (S3 keys, bucket names, exception strings, message IDs, and parsed message body keys) are escaped before being written to logs, so control characters or newlines in a crafted value cannot forge or inject CloudWatch log lines.

## CloudWatch Custom Metrics

When `EmitMetrics` is set to `true`, the Lambda emits custom metrics via Amazon CloudWatch Embedded Metric Format (EMF) under the **`SAP/LogServ`** namespace. No additional infrastructure is required — metrics are extracted from log output automatically.

### Per-File Metrics

| Metric | Unit | Description |
|--------|------|-------------|
| `FilesProcessed` | Count | 1 per file successfully forwarded |
| `FilesFiltered` | Count | 1 per file skipped (category filter or size) |
| `FilesFailed` | Count | 1 per file that errored |
| `ProcessingTime` | Milliseconds | Time to download, decompress, and upload a single file |
| `FileSize` | Bytes | Decompressed file size written to destination |

### Per-Batch Metrics

| Metric | Unit | Description |
|--------|------|-------------|
| `BatchSize` | Count | Number of SQS messages in the Lambda invocation |
| `BatchProcessingTime` | Milliseconds | Total invocation duration (end-to-end) |
| `FilesFiltered` | Count | Total files filtered in the batch |
| `FilesFailed` | Count | Total files that errored in the batch |

### Dimensions

| Dimension | Example Values | Purpose |
|-----------|---------------|---------|
| `Environment` | dev, prod, *(blank)* | Separate metrics by deployment |
| `Category` | abap, hana, linux, sap, webdispatcher | Volume/latency by log type |

### Lambda Cost-Effectiveness Analysis

The primary use of these metrics is to determine when Lambda becomes less cost-effective than an always-on container (ECS Fargate) or a provisioned Lambda instance. The key metric for this analysis is **files per minute** (`FilesProcessed` Sum over 1-minute periods), not SQS messages per minute, because Lambda cost is driven by compute duration — and compute duration is proportional to the number of files downloaded, decompressed, and uploaded.

#### Why Files Per Minute

| Factor | Why It Matters |
|--------|---------------|
| Lambda cost model | Billed per invocation + (duration × memory). Each file incurs download/decompress/upload time. |
| SQS messages ≠ work done | One SQS message can contain multiple S3 event records. Message count understates or overstates actual compute consumed. |
| Container cost model | Fixed vCPU + memory cost regardless of throughput. Higher sustained throughput amortises the fixed cost. |

#### Break-Even Calculation

```
Lambda monthly cost ≈ (files/month × avg_ProcessingTime_sec × 0.512 GB × $0.0000166667/GB-sec)
                    + (invocations/month × $0.20 / 1,000,000)

Container monthly cost ≈ vCPU_hours × $0.04048/hr + GB_hours × $0.004445/hr  (Fargate pricing)
```

When sustained `FilesProcessed` Sum (1-min) consistently exceeds the crossover threshold for hours at a time, a container becomes cheaper. Use the following dashboard widgets to monitor:

#### Recommended CloudWatch Dashboard Widgets

| Widget | Metric / Statistic | Purpose |
|--------|-------------------|---------|
| Files per minute | `FilesProcessed` Sum, 1-min period | Primary throughput signal for cost crossover |
| Avg processing time | `ProcessingTime` Average, 5-min period | Cost-per-file indicator |
| Batch utilisation | `BatchSize` Average, 5-min period | SQS batching efficiency — low values (<3) mean excess invocation overhead |
| Sustained throughput | `FilesProcessed` Sum, 1-hour period | Long-window view for trend analysis |
| P99 processing time | `ProcessingTime` p99, 5-min period | Tail latency / large file detection |
| Error rate | `FilesFailed` Sum / (`FilesProcessed` Sum + `FilesFailed` Sum) | Pipeline health |
| Throughput (bytes/sec) | `FileSize` Sum / period seconds | Data volume indicator |

#### When to Consider Migrating Off Lambda

Monitor the following signals over a 7-day rolling window:

1. **Sustained high throughput** — `FilesProcessed` Sum (1-hour) consistently above ~3,000 files/hour (50 files/min) for 18+ hours/day.
2. **High batch utilisation already** — `BatchSize` Average near 10 (maximum), meaning you cannot reduce invocations further by increasing batch size.
3. **Stable, predictable volume** — Low variance in hourly file counts (coefficient of variation < 0.3), indicating the workload doesn't benefit from Lambda's pay-per-use elasticity.
4. **Cost projection exceeds container equivalent** — Monthly Lambda bill > $50–80 (the approximate cost of a 0.5 vCPU / 1GB Fargate task running 24/7).

If none of these conditions are met, Lambda remains the more cost-effective option due to zero idle cost, no operational overhead, and automatic scaling.

## Security

### Input Validation

| Control | Description |
|---------|-------------|
| **S3 key format validation** | All S3 keys are validated against expected path patterns before processing. Keys must match `logserv/<category>/<subcategory>/...` or `AWSLogs/<account-id>/...`. Path traversal (`../`), backslashes, and control characters are rejected. |
| **Strict message parsing** | Only standard S3 event notification format is accepted. The Lambda does not process arbitrary JSON payloads — messages must contain valid `Records[].s3.bucket.name` and `Records[].s3.object.key` structures. |
| **Source bucket pinning** | The bucket named in the S3 event is checked against an allowlist (`SOURCE_BUCKET_NAME` plus `SOURCE_BUCKET_ALLOWLIST`) before the object is read. Events naming any other bucket are rejected. Disabled if the allowlist is empty (a warning is logged at cold start). |
| **File size limit** | Files exceeding `MaxFileSizeMB` (default 50MB) are rejected. The limit is enforced against the object's real `ContentLength` before the body is read into memory; the event-reported size is used only as an advisory pre-filter. |
| **Decompression bomb protection** | Streaming decompression aborts if output exceeds `MAX_DECOMPRESSED_SIZE_MB` (default: 10× `MaxFileSizeMB` = 500MB). Prevents a small malicious `.gz` file from exhausting Lambda memory. |
| **Fail on undecompressable input** | A file that signals gzip but fails to decompress is treated as an error (retried/DLQ), not stored as-is under a `.json` name. |
| **Log sanitisation** | Untrusted values (S3 keys, bucket names, exception strings, message IDs, parsed body keys) are escaped before logging so control characters or newlines cannot forge CloudWatch log lines. |

### IAM & Access Control

| Control | Description |
|---------|-------------|
| **Least-privilege execution role** | Lambda role grants only `s3:PutObject` on the destination bucket, `s3:GetObject` on the source bucket, and scoped SQS permissions on the specific queue ARN. |
| **Principal-scoped cross-account access** | `AllowedPrincipalArns` accepts specific IAM role/user ARNs — not account-root principals — ensuring only designated identities can read from the destination bucket. |
| **SecureTransport condition** | Cross-account read access is conditional on `aws:SecureTransport: true`, preventing unencrypted access. |

### Data Protection

| Control | Description |
|---------|-------------|
| **Encryption at rest (S3)** | Destination bucket uses AES-256 server-side encryption (SSE-S3) by default. |
| **Encryption at rest (SQS)** | Dead Letter Queue uses SQS-managed server-side encryption (SSE-SQS). |
| **Key management (SSE-KMS)** | *Known limitation.* The template does not expose a parameter for SSE-KMS with a customer-managed key (CMK). See [Using a customer-managed KMS key](#using-a-customer-managed-kms-key) below if your compliance framework requires one. |
| **Network (VPC)** | The Lambda runs outside a VPC and reaches the cross-account SQS queue and S3 buckets over public AWS service endpoints (TLS 1.2+). See [Running the Lambda in a VPC](#running-the-lambda-in-a-vpc) below if private networking is required. |
| **Versioning** | Destination bucket has versioning enabled for object recovery and auditability. |
| **Encryption in transit** | Bucket policy enforces TLS 1.2+ and denies insecure transport. |
| **Public access blocked** | All four S3 public access block settings are enabled. |
| **Access logging** | Optional. When `AccessLogBucketName` is provided, S3 server access logging records all requests to the destination bucket. See [Enabling S3 Access Logging](#enabling-s3-access-logging). |

#### Using a customer-managed KMS key

The destination bucket is encrypted with SSE-S3 (`AES256`), hardcoded in the `LogServDestBucket` resource in `template.yaml`. There is no parameter to switch it to SSE-KMS, and the dead-letter queue uses the AWS-managed `alias/aws/sqs` key rather than a customer-managed key.

If your compliance framework (for example SOX, GDPR, or an industry-specific regime) requires a customer-managed KMS key, you have two options:

1. **Modify the template.** Change the destination bucket's `BucketEncryption` to use `aws:kms` and reference your key:
   ```yaml
   BucketEncryption:
     ServerSideEncryptionConfiguration:
       - ServerSideEncryptionByDefault:
           SSEAlgorithm: aws:kms
           KMSMasterKeyID: <your-kms-key-arn>
         BucketKeyEnabled: true
   ```
2. **Bring your own bucket.** Set `CreateDestBucket=false` and point the forwarder at an existing bucket that already has an SSE-KMS default encryption rule.

In **both** cases you must extend the `LogForwarderExecutionRole` (in `template.yaml`) with permission on your key, because the reference role does not grant any KMS actions:

```yaml
- PolicyName: DestKmsAccess
  PolicyDocument:
    Version: "2012-10-17"
    Statement:
      - Effect: Allow
        Action:
          - kms:GenerateDataKey
          - kms:Decrypt
        Resource: <your-kms-key-arn>
```

If cross-account principals (`AllowedPrincipalArns`) read the objects, grant them usage of the same key through the key policy as well.

#### Running the Lambda in a VPC

By default the forwarder Lambda function is **not** attached to a VPC (there is no `VpcConfig` in `template.yaml`). It communicates only with Amazon SQS and Amazon S3 over public AWS service endpoints, secured with TLS 1.2+, and requires no access to private VPC resources. Running outside a VPC is the recommended posture for this workload: it avoids elastic network interface (ENI) cold-start latency and does not consume subnet IP addresses.

Attach the function to a VPC only if your security posture requires AWS API traffic to stay on private networking. If you do, you must also:

- Add a `VpcConfig` (subnets and a security group) to the `LogForwarderFunction` resource.
- Provide an **Amazon S3 gateway VPC endpoint** and an **Amazon SQS interface VPC endpoint** in those subnets, so the function can reach the source queue and both buckets without internet egress.
- Ensure the subnets have sufficient free IP addresses for the function's peak concurrency.

The reference template does not configure any of this.

### Monitoring & Alerting

| Control | Description |
|---------|-------------|
| **DLQ alarm** | Triggers when any message lands in the Dead Letter Queue. Sends notifications to the configured SNS topic (`AlarmNotificationTopicArn`). |
| **Error alarm** | Triggers when Lambda errors exceed 10 per 5-minute window. Sends notifications to the configured SNS topic. |
| **Throttle alarm** | Triggers when Lambda throttles exceed 5 per 5-minute window. Sends notifications to the configured SNS topic. |
| **Structured logging** | All operations are logged with source/dest paths and processing metadata. |

### Environment Variables

The Lambda reads the following environment variables that are not exposed as CloudFormation parameters:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_DECOMPRESSED_SIZE_MB` | `500` (10× `MaxFileSizeMB`) | Maximum allowed decompressed file size in MB. Files exceeding this are skipped as potential decompression bombs. |
| `SOURCE_BUCKET_ALLOWLIST` | *(blank)* | Comma-separated source buckets allowed in addition to `SourceBucketName`. Used when a single deployment reads from more than one CLZ bucket. |
| `SKIP_IF_EXISTS` | `true` | Use a conditional PUT (`IfNoneMatch="*"`) so each key is written at most once, for idempotency on SQS redelivery. Set to `false` to always overwrite. |

## Cross-Account Access

The source S3 bucket (`sap-hec-clz-*`) has a bucket policy that grants the target account read access.

The SQS queue policy grants the target account receive/delete/get-attributes permissions.

The destination bucket optionally grants read access to specific IAM principals in other accounts via the `AllowedPrincipalArns` parameter. Access is:
- Scoped to specific IAM role/user ARNs (not account-root)
- Restricted to the `DestPrefix` path only
- Conditional on secure transport (TLS)

## File Format

Files arrive as gzip-compressed NDJSON (`.json.gz`). After decompression, each line contains:

```json
{
  "_raw": "<actual raw log content>",
  "_time": <unix_epoch_timestamp>,
  "source": "<original log file path>",
  "host": "<hostname>",
  "clz_dir": "<log category>",
  "clz_subdir": "<log subcategory>",
  "clzfilename": "<original filename>"
}
```

The **`_raw` field** contains the original log line content exactly as written by the source application/system. Its format varies by log type.


## Cost Estimate

Estimated monthly costs for a typical SAP RISE landscape (single SID, ~27 log subcategories, ~20GB/day raw log volume):

### Lambda

| Component | Estimate | Basis |
|-----------|----------|-------|
| Invocations | ~$0.50 | ~2.5M invocations/month (batch size 10, ~25K files/day) |
| Duration | ~$5–15 | 512MB × ~500ms avg × 2.5M invocations (arm64 pricing) |
| **Lambda total** | **~$6–16/month** | |

### S3 Storage

| Component | Estimate | Basis |
|-----------|----------|-------|
| Storage (Standard) | ~$14 | ~600GB/month (decompressed, before lifecycle transitions) |
| Storage (Intelligent-Tiering) | ~$5–8 | After 30-day transition for older logs |
| PUT requests | ~$3 | ~750K PUT requests/month |
| GET requests | Varies | Depends on downstream consumption (Athena, OpenSearch, etc.) |
| **S3 total** | **~$20–25/month** | With 365-day retention |

### CloudWatch

| Component | Estimate | Basis |
|-----------|----------|-------|
| Custom metrics (EMF) | < $15 | ~10–50 metrics (2 dimensions × 5 metric types) |
| Log ingestion | ~$3–5 | Lambda logs (~1GB/month at structured INFO level) |
| Alarms | < $1 | 3 alarms (DLQ, errors, throttles) |
| **CloudWatch total** | **~$15–20/month** | When `EmitMetrics` is enabled |

### SQS

| Component | Estimate | Basis |
|-----------|----------|-------|
| Requests | < $1 | Included in free tier for most volumes |

### Total Estimated Cost

| Scenario | Monthly Estimate |
|----------|-----------------|
| Metrics disabled | **~$25–40/month** |
| Metrics enabled | **~$40–60/month** |

*Costs scale linearly with log volume. For multi-SID landscapes, multiply by the number of active SIDs. Actual costs depend on region, data volume, and access patterns. Use the [AWS Pricing Calculator](https://calculator.aws/) for precise estimates.*

## Storage Optimization

The default lifecycle configuration transitions objects to S3 Intelligent-Tiering after 30 days, which automatically moves data between frequent and infrequent access tiers based on usage patterns. However, for log data that is write-once and rarely accessed directly from S3, transitioning to S3 Standard-Infrequent Access (S3 Standard-IA) can provide significant cost savings.

### Recommended Storage Strategies

| Strategy | Best For | Configuration | Savings vs Standard |
|----------|----------|--------------|---------------------|
| **Default (Intelligent-Tiering)** | Unknown or mixed access patterns | Included in template (30-day transition) | ~40% on older data |
| **S3 Standard-IA (immediate)** | Logs ingested into SIEM/analytics on arrival, rarely re-read from S3 | Transition to IA at 0 days | ~40% from day 1 |
| **S3 Standard-IA (delayed)** | Logs queried directly from S3 for first few days, then idle | Transition to IA after 7–14 days | ~40% after transition |
| **S3 Standard-IA + Glacier** | Long-term compliance retention | IA at 0–14 days, Glacier at 90 days | ~70–80% on archived data |

### When to Use S3 Standard-IA

S3 Standard-IA is recommended when:
- Logs are primarily ingested into a SIEM or analytics platform (OpenSearch, Athena, Splunk) shortly after arrival and rarely re-read from S3 directly
- Direct S3 access is only needed during incident investigations (infrequent by definition)
- You want predictable costs without the Intelligent-Tiering monitoring fee ($0.0025 per 1,000 objects/month)
- Minimum object size is not a concern (log files from LogServ are typically >128KB decompressed)

> **Note (July 2026):** AWS has [removed the previous 30-day minimum retention requirement](https://aws.amazon.com/about-aws/whats-new/2026/07/s3-removes-30-day-transitions-standard-ia-one-zone-ia/) for S3 Standard-IA transitions. Objects can now be transitioned to IA as soon as the day they are created (0 days), making it ideal for write-once log workloads.

S3 Standard-IA is **not recommended** when:
- Logs are frequently queried directly from S3 (e.g., Athena queries running daily against raw log files) — IA has a per-GB retrieval fee
- Object sizes are very small (<128KB) — S3 Standard-IA has a 128KB minimum charge per object

### Customizing the Lifecycle Policy

To use S3 Standard-IA instead of (or alongside) Intelligent-Tiering, modify the `LifecycleConfiguration` in `template.yaml`:

**Immediate IA transition (best for SIEM-ingested logs):**
```yaml
LifecycleConfiguration:
  Rules:
    - Id: LogRetention
      Status: Enabled
      ExpirationInDays: !Ref RetentionDays
    - Id: TransitionToIA
      Status: Enabled
      Transitions:
        - StorageClass: STANDARD_IA
          TransitionInDays: 0
```

**Delayed IA transition (keep Standard for first 7 days):**
```yaml
LifecycleConfiguration:
  Rules:
    - Id: LogRetention
      Status: Enabled
      ExpirationInDays: !Ref RetentionDays
    - Id: TransitionToIA
      Status: Enabled
      Transitions:
        - StorageClass: STANDARD_IA
          TransitionInDays: 7
    # Optional: archive to Glacier for long-term compliance
    # - Id: TransitionToGlacier
    #   Status: Enabled
    #   Transitions:
    #     - StorageClass: GLACIER
    #       TransitionInDays: 90
```

### Cost Comparison (600GB/month, 365-day retention)

| Storage Class | Monthly Cost | Notes |
|--------------|-------------|-------|
| Standard (all data) | ~$14 | No lifecycle transitions |
| Intelligent-Tiering (default) | ~$8–10 | Auto-tiers after 30 days; monitoring fee applies |
| Standard-IA from day 0 | ~$5–6 | Best for write-once-read-rarely; no monitoring fee |
| Standard (7d) → IA (358d) | ~$6–7 | Brief Standard period for immediate queries |
| Standard-IA (0d) → Glacier (90d) | ~$3–4 | Maximum savings; Glacier retrieval takes minutes–hours |

## Notices

Customers are responsible for making their own independent assessment of the information in this Guidance. This Guidance: (a) is for informational purposes only, (b) represents AWS current product offerings and practices, which are subject to change without notice, and (c) does not create any commitments or assurances from AWS and its affiliates, suppliers or licensors. AWS products or services are provided "as is" without warranties, representations, or conditions of any kind, whether express or implied. AWS responsibilities and liabilities to its customers are controlled by AWS agreements, and this Guidance is not part of, nor does it modify, any agreement between AWS and its customers.