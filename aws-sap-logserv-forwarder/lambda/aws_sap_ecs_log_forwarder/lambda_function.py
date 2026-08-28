"""
SAP LogServ Log Forwarder Lambda

Processes S3 event notifications from SQS, applies include/exclude filtering,
decompresses gzipped log files (json.gz format), and copies qualifying log
files to the destination bucket.

LogServ delivers logs in TCP JSON compressed format (json.gz). Each file
contains NDJSON (newline-delimited JSON) with fields like:
  {"_raw":"<actual log line>","_time":<epoch>,"clz_dir":"<category>",
   "clz_subdir":"<subcategory>","source":"<path>","host":"<hostname>"}

Source: SAP ECS CLZ (Customer Landing Zone) S3 bucket with SQS notifications.
Architecture: DLZ -> CLZ -> Object Store (S3) -> SQS -> This Lambda -> Dest S3

Environment Variables:
    DEST_BUCKET_NAME        - Destination S3 bucket (required)
    SOURCE_BUCKET_NAME      - Source S3 bucket. If set, the bucket named in the S3 event
                              must match it (pin); mismatched events are rejected. If unset
                              (and SOURCE_BUCKET_ALLOWLIST is empty), pinning is disabled.
    SOURCE_BUCKET_ALLOWLIST - Comma-separated additional allowed source buckets (optional)
    INCLUDE_CATEGORIES      - Comma-separated list of log categories to include (default: all)
                              Categories: abap, dns, hana, linux, sap, scc, webdispatcher,
                              java, bobj_bi, bobj_bods, bobj_sacagent, sybase, windows
    EXCLUDE_SUBCATEGORIES   - Comma-separated list of subcategories to exclude
                              e.g. "audit,proxy,slapd"
    DEST_PREFIX             - Prefix to prepend in destination bucket (default: "logserv/")
    LOG_LEVEL               - Logging level (default: INFO)
    MAX_FILE_SIZE_MB        - Maximum file size to process in MB (default: 50). Enforced
                              authoritatively against the real object size before download.
    MAX_DECOMPRESSED_SIZE_MB- Max decompressed size in MB (default: MAX_FILE_SIZE_MB * 10)
    DECOMPRESS              - Whether to decompress .gz files (default: "true")
    SKIP_IF_EXISTS          - Idempotency on SQS redelivery: when "true" (default),
                              writes use a conditional PUT (IfNoneMatch="*") so an
                              object is written at most once per key. Needs only
                              s3:PutObject on the destination.
"""

import json
import gzip
import logging
import os
import re
import time
from io import BytesIO
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# --- Configuration ---
DEST_BUCKET = os.environ["DEST_BUCKET_NAME"]
SOURCE_BUCKET_OVERRIDE = os.environ.get("SOURCE_BUCKET_NAME", "")
SOURCE_BUCKET_ALLOWLIST = [
    b.strip()
    for b in os.environ.get("SOURCE_BUCKET_ALLOWLIST", "").split(",")
    if b.strip()
]
# Buckets this function is permitted to read from. SOURCE_BUCKET_NAME (if set)
# is treated as a pin against the event-supplied bucket, not a silent rewrite.
# If this set is empty, source-bucket pinning is disabled (fail-open) and a
# warning is emitted at cold start. See threat T11 (confused deputy).
ALLOWED_SOURCE_BUCKETS = set(SOURCE_BUCKET_ALLOWLIST)
if SOURCE_BUCKET_OVERRIDE:
    ALLOWED_SOURCE_BUCKETS.add(SOURCE_BUCKET_OVERRIDE)
INCLUDE_CATEGORIES = [
    c.strip().lower()
    for c in os.environ.get("INCLUDE_CATEGORIES", "").split(",")
    if c.strip()
]
EXCLUDE_SUBCATEGORIES = [
    c.strip().lower()
    for c in os.environ.get("EXCLUDE_SUBCATEGORIES", "").split(",")
    if c.strip()
]
DEST_PREFIX = os.environ.get("DEST_PREFIX", "logserv/")
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))
MAX_DECOMPRESSED_SIZE_MB = int(os.environ.get("MAX_DECOMPRESSED_SIZE_MB", str(MAX_FILE_SIZE_MB * 10)))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
DECOMPRESS = os.environ.get("DECOMPRESS", "true").lower() == "true"
EMIT_METRICS = os.environ.get("EMIT_METRICS", "false").lower() == "true"
FORWARD_AWS_LOGS = os.environ.get("FORWARD_AWS_LOGS", "true").lower() == "true"
ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")
# When true, skip upload if the destination object already exists (idempotency
# on SQS redelivery). See threat T14.
SKIP_IF_EXISTS = os.environ.get("SKIP_IF_EXISTS", "true").lower() == "true"

# --- Logging ---
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

if not ALLOWED_SOURCE_BUCKETS:
    logger.warning(
        "Source-bucket pinning disabled: neither SOURCE_BUCKET_NAME nor "
        "SOURCE_BUCKET_ALLOWLIST is set. The function will read from whatever "
        "bucket the S3 event names, bounded only by the execution role. Set "
        "SOURCE_BUCKET_NAME to pin the source bucket (mitigates confused-deputy reads)."
    )


def _sanitize_for_log(value):
    """
    Make an untrusted string safe to place in a log record.

    S3 keys can contain newlines and control characters (which is exactly why
    invalid keys are rejected). Because those keys are logged on the rejection
    path, escape control characters first to prevent CloudWatch log-line
    injection / forgery. Returns a repr-style escaped string.
    """
    if not isinstance(value, str):
        value = str(value)
    # backslash-escape control chars (incl. \r \n) and non-printables
    return value.encode("unicode_escape").decode("ascii")

# --- S3 Client with retry ---
s3_config = Config(
    retries={"max_attempts": 5, "mode": "adaptive"},
    max_pool_connections=25,
)
s3 = boto3.client("s3", config=s3_config)


def emit_emf(metric_name, value, unit, dimensions, additional_fields=None):
    """Emit a CloudWatch Embedded Metric Format log entry."""
    if not EMIT_METRICS:
        return
    entry = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "SAP/LogServ",
                "Dimensions": [list(dimensions.keys())],
                "Metrics": [{"Name": metric_name, "Unit": unit}],
            }],
        },
        metric_name: value,
        **dimensions,
    }
    if additional_fields:
        entry.update(additional_fields)
    print(json.dumps(entry))


class DecompressionBombError(Exception):
    """Raised when decompressed output exceeds the safe size limit."""
    pass


class FileTooLargeError(Exception):
    """Raised when the real object size exceeds the configured limit."""
    pass


class DecompressionFailedError(Exception):
    """Raised when a file that appears gzipped cannot be decompressed."""
    pass


def _safe_decompress(compressed_data, max_bytes):
    """
    Decompress gzip data with a size limit to prevent decompression bombs.

    Uses streaming decompression that aborts as soon as the output exceeds
    max_bytes, preventing OOM on maliciously crafted .gz files.
    """
    buf = BytesIO(compressed_data)
    output = BytesIO()
    bytes_written = 0

    with gzip.GzipFile(fileobj=buf, mode="rb") as gz:
        while True:
            chunk = gz.read(65536)  # 64KB chunks
            if not chunk:
                break
            bytes_written += len(chunk)
            if bytes_written > max_bytes:
                raise DecompressionBombError(
                    f"Decompressed size exceeds {max_bytes // (1024*1024)}MB limit "
                    f"(read {bytes_written // (1024*1024)}MB so far). "
                    f"Possible decompression bomb."
                )
            output.write(chunk)

    return output.getvalue()


# --- S3 Key Validation ---
# Matches expected LogServ paths: logserv/<category>/<subcategory>/.../<filename>
# or AWSLogs/<account-id>/... paths
# Rejects: path traversal (../), null bytes, control characters, backslashes
_VALID_LOGSERV_KEY_RE = re.compile(
    r"^(logserv/[a-z0-9_]+/[a-z0-9_]+/.+|AWSLogs/\d{12}/.+)$",
    re.IGNORECASE,
)
_DANGEROUS_PATTERNS_RE = re.compile(r"(\.\./|\\|[\x00-\x1f])")


def _validate_s3_key(key):
    """
    Validate that an S3 key matches expected LogServ or AWSLogs path patterns.

    Rejects keys with path traversal sequences, backslashes, or control characters.
    Returns True if valid, False otherwise.
    """
    if not key:
        return False
    if _DANGEROUS_PATTERNS_RE.search(key):
        return False
    if not _VALID_LOGSERV_KEY_RE.match(key):
        return False
    return True


def lambda_handler(event, context):
    """
    Process SQS batch of S3 event notifications.

    Each SQS message contains one or more S3 event records.
    Returns partial batch failure response for failed records.
    """
    batch_start = time.time()
    batch_item_failures = []
    records_processed = 0
    records_filtered = 0
    records_failed = 0

    # Emit batch size metric (number of SQS messages received per invocation)
    batch_size = len(event.get("Records", []))
    emit_emf("BatchSize", batch_size, "Count",
             {"Environment": ENVIRONMENT, "Category": "all"})

    for sqs_record in event.get("Records", []):
        message_id = sqs_record.get("messageId", "unknown")
        message_failed = False
        try:
            s3_events = parse_sqs_message(sqs_record)
        except Exception as e:
            logger.error(
                "Failed to parse SQS message",
                extra={
                    "message_id": _sanitize_for_log(message_id),
                    "error": _sanitize_for_log(str(e)),
                },
                exc_info=True,
            )
            batch_item_failures.append({"itemIdentifier": message_id})
            records_failed += 1
            continue

        for s3_event in s3_events:
            source_bucket = s3_event["bucket"]
            source_key = s3_event["key"]
            file_size = s3_event.get("size", 0)

            # Validate S3 key format (reject path traversal, unexpected patterns)
            if not _validate_s3_key(source_key):
                logger.warning(
                    "Rejected invalid S3 key",
                    extra={
                        "key": _sanitize_for_log(source_key),
                        "reason": "failed format validation",
                    },
                )
                records_filtered += 1
                continue

            # Check file size limit (advisory pre-check from event metadata;
            # the authoritative check is against the real object size in
            # process_file, since event size is attacker-influenceable).
            if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                logger.warning(
                    "Skipping oversized file",
                    extra={
                        "key": _sanitize_for_log(source_key),
                        "size_mb": round(file_size / (1024 * 1024), 2),
                        "max_mb": MAX_FILE_SIZE_MB,
                    },
                )
                records_filtered += 1
                continue

            # Apply include/exclude filters
            if not passes_filter(source_key):
                logger.debug(
                    "Filtered out", extra={"key": _sanitize_for_log(source_key)}
                )
                records_filtered += 1
                continue

            # Process each S3 record in isolation. A failure on one record must
            # not abort processing of its siblings in the same SQS message.
            # process_file is idempotent (conditional write), so if the message
            # is retried, already-copied siblings are not re-uploaded. (T14)
            try:
                process_file(source_bucket, source_key)
                records_processed += 1
            except (FileTooLargeError, DecompressionFailedError) as e:
                # Permanent failures — the same object will fail identically on
                # every redelivery. Do NOT set message_failed: retrying would
                # burn invocations and re-process healthy siblings on each
                # redelivery (poison-pill / retry storm). Filter and move on;
                # the condition is logged and surfaced via the FilesFiltered
                # metric. (T13 oversized, T15 undecompressable)
                logger.error(
                    "Skipping object — permanent processing failure",
                    extra={
                        "key": _sanitize_for_log(source_key),
                        "error": _sanitize_for_log(str(e)),
                    },
                )
                records_filtered += 1
                emit_emf("FilesFiltered", 1, "Count",
                         {"Environment": ENVIRONMENT, "Category": "permanent_failure"})
            except Exception as e:
                logger.error(
                    "Failed to process S3 record",
                    extra={
                        "message_id": _sanitize_for_log(message_id),
                        "key": _sanitize_for_log(source_key),
                        "error": _sanitize_for_log(str(e)),
                    },
                    exc_info=True,
                )
                message_failed = True
                records_failed += 1

        # SQS partial-batch-failure granularity is per-message: if any record in
        # this message failed transiently, return the whole message for retry.
        if message_failed:
            batch_item_failures.append({"itemIdentifier": message_id})

    logger.info(
        "Batch complete",
        extra={
            "processed": records_processed,
            "filtered": records_filtered,
            "failed": records_failed,
            "remaining_ms": context.get_remaining_time_in_millis(),
        },
    )

    # Emit batch-level metrics via EMF
    batch_elapsed = round((time.time() - batch_start) * 1000)
    emit_emf("BatchProcessingTime", batch_elapsed, "Milliseconds",
             {"Environment": ENVIRONMENT, "Category": "all"})
    if records_failed > 0:
        emit_emf("FilesFailed", records_failed, "Count",
                 {"Environment": ENVIRONMENT, "Category": "all"})
    if records_filtered > 0:
        emit_emf("FilesFiltered", records_filtered, "Count",
                 {"Environment": ENVIRONMENT, "Category": "all"})

    return {"batchItemFailures": batch_item_failures}


def parse_sqs_message(sqs_record):
    """
    Parse an SQS record to extract S3 event notifications.

    Handles both direct S3 notifications and SNS-wrapped notifications.
    """
    body = json.loads(sqs_record["body"])

    # Handle SNS-wrapped messages
    if "Message" in body and "TopicArn" in body:
        body = json.loads(body["Message"])

    s3_events = []

    for record in body.get("Records", []):
        if record.get("eventSource") != "aws:s3":
            continue

        bucket_name = record["s3"]["bucket"]["name"]
        object_key = unquote_plus(record["s3"]["object"]["key"])
        object_size = record["s3"]["object"].get("size", 0)

        # Pin the source bucket: if an allowlist is configured, the bucket named
        # in the (attacker-influenceable) event must be on it. This is a reject,
        # not a silent rewrite — a mismatch means the event is not for us.
        # Mitigates confused-deputy reads from arbitrary buckets (T11).
        if ALLOWED_SOURCE_BUCKETS and bucket_name not in ALLOWED_SOURCE_BUCKETS:
            logger.warning(
                "Rejected S3 event: source bucket not in allowlist",
                extra={"bucket": _sanitize_for_log(bucket_name)},
            )
            continue

        s3_events.append(
            {"bucket": bucket_name, "key": object_key, "size": object_size}
        )

    # Handle case where body itself is a single S3 event (non-standard format)
    # SECURITY: Removed permissive fallback parser that accepted arbitrary JSON
    # with "bucket"/"key" fields. Only standard S3 event notification format
    # (with Records[].s3.bucket.name and Records[].s3.object.key) is accepted.
    if not s3_events:
        logger.warning(
            "SQS message contained no valid S3 event records",
            extra={
                "body_keys": (
                    [_sanitize_for_log(k) for k in body.keys()]
                    if isinstance(body, dict)
                    else "non-dict"
                )
            },
        )

    return s3_events


def passes_filter(key):
    """
    Check if a key passes the include/exclude filters.

    Key structure: logserv/<category>/<subcategory>/<date>/<filename>
    Example: logserv/abap/dispatcher/2026/01/09/dev_disp-35FyFY.json
    """
    # Allow AWSLogs prefix through (VPC flow logs, ELB logs and WAF logs)
    if key.startswith("AWSLogs/"):
        return FORWARD_AWS_LOGS

    # Must contain 'logserv' in path (relevance check)
    if "logserv" not in key.lower():
        return False

    parts = key.lower().split("/")

    # Extract category and subcategory from path
    # Expected: logserv/<category>/<subcategory>/...
    category = ""
    subcategory = ""

    try:
        logserv_idx = next(i for i, p in enumerate(parts) if p == "logserv")
        if len(parts) > logserv_idx + 1:
            category = parts[logserv_idx + 1]
        if len(parts) > logserv_idx + 2:
            subcategory = parts[logserv_idx + 2]
    except StopIteration:
        return False

    # Include filter: if set, category must be in the list
    if INCLUDE_CATEGORIES and category not in INCLUDE_CATEGORIES:
        return False

    # Exclude filter: if subcategory matches, skip
    if EXCLUDE_SUBCATEGORIES and subcategory in EXCLUDE_SUBCATEGORIES:
        return False

    return True


def extract_category(key):
    """Extract category and subcategory from a logserv key path."""
    parts = key.lower().split("/")
    category = "unknown"
    subcategory = "unknown"
    try:
        idx = next(i for i, p in enumerate(parts) if p == "logserv")
        if len(parts) > idx + 1:
            category = parts[idx + 1]
        if len(parts) > idx + 2:
            subcategory = parts[idx + 2]
    except StopIteration:
        pass
    return category, subcategory


def process_file(source_bucket, source_key):
    """
    Download file from source, decompress if gzipped, and upload to destination.

    LogServ delivers files in json.gz format (gzip-compressed NDJSON).
    We decompress them and store as plain .json in the destination bucket,
    preserving the original key structure.
    """
    start = time.time()

    # Determine destination key (strip .gz extension if decompressing)
    dest_key = source_key
    if DECOMPRESS and dest_key.endswith(".gz"):
        dest_key = dest_key[:-3]
    if DEST_PREFIX and not dest_key.startswith(DEST_PREFIX):
        # If key already starts with logserv/, don't double-prefix
        if not dest_key.startswith("logserv/"):
            dest_key = DEST_PREFIX + dest_key

    # Authoritative size check against the REAL object size, before reading the
    # body into memory. The event-supplied size is attacker-influenceable, so it
    # cannot be trusted to gate the in-memory read. (T13)
    response = s3.get_object(Bucket=source_bucket, Key=source_key)
    body = response["Body"]
    try:
        content_length = response.get("ContentLength")
        max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
        if content_length is None:
            # ContentLength is a standard GET response header and should always
            # be present. If it is absent we cannot vet the size up front, so
            # read with a hard cap (max_bytes + 1) and reject if the object is
            # larger — never do an unbounded read. (T13 fail-closed)
            content = body.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise FileTooLargeError(
                    f"Object exceeds {MAX_FILE_SIZE_MB}MB limit "
                    f"(ContentLength absent; bounded read)"
                )
        elif content_length > max_bytes:
            raise FileTooLargeError(
                f"Object size {content_length} bytes exceeds "
                f"{MAX_FILE_SIZE_MB}MB limit (authoritative check)"
            )
        else:
            content = body.read()
    finally:
        # Release the connection back to the pool even on the reject path, so a
        # burst of oversized objects cannot exhaust max_pool_connections.
        body.close()
    content_encoding = response.get("ContentEncoding", "")

    # Decompress if enabled and file appears to be gzipped
    if DECOMPRESS:
        is_gzipped = (
            source_key.endswith(".gz")
            or content_encoding == "gzip"
            or (len(content) >= 2 and content[0] == 0x1F and content[1] == 0x8B)
        )

        if is_gzipped:
            try:
                max_decompressed_bytes = MAX_DECOMPRESSED_SIZE_MB * 1024 * 1024
                content = _safe_decompress(content, max_decompressed_bytes)
            except DecompressionBombError as e:
                # Permanent condition: the same object will always exceed the
                # limit. Raise so the handler filters it (not counted as
                # processed, not retried). (T13)
                logger.error(
                    "Decompression bomb detected — file skipped",
                    extra={
                        "key": _sanitize_for_log(source_key),
                        "error": _sanitize_for_log(str(e)),
                    },
                )
                # Counted/emitted once by the handler's permanent-failure path.
                raise DecompressionFailedError(str(e)) from e
            except DecompressionFailedError:
                raise
            except Exception as e:
                # A file that looked gzipped but won't decompress is corrupt or
                # mislabeled. Do NOT silently store raw bytes as application/json
                # (that mislabels/taints the destination). Fail the record so it
                # is surfaced (DLQ) rather than persisted as valid JSON. (T15)
                raise DecompressionFailedError(
                    f"gzip-signalled object failed to decompress: {e}"
                ) from e

    # Upload to destination.
    # Idempotency (T14): when SKIP_IF_EXISTS is set, use a conditional write
    # (IfNoneMatch="*") so the PUT succeeds only if the key does not already
    # exist. This is atomic on the S3 side — unlike a HeadObject-then-Put it
    # closes the race between concurrent invocations processing the same object
    # (SQS at-least-once redelivery), and it needs only s3:PutObject (no
    # s3:GetObject/s3:ListBucket on the destination). A pre-existing object
    # yields 412 PreconditionFailed, which we treat as "already written".
    put_kwargs = {
        "Bucket": DEST_BUCKET,
        "Key": dest_key,
        "Body": content,
        "ContentType": "application/json",
    }
    if SKIP_IF_EXISTS:
        put_kwargs["IfNoneMatch"] = "*"
    try:
        s3.put_object(**put_kwargs)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if SKIP_IF_EXISTS and (code == "PreconditionFailed" or status == 412):
            logger.info(
                "Destination object already exists — skipping (idempotent)",
                extra={"key": _sanitize_for_log(dest_key)},
            )
            return
        raise

    elapsed = round((time.time() - start) * 1000)
    logger.info(
        "Processed file",
        extra={
            "source": _sanitize_for_log(f"s3://{source_bucket}/{source_key}"),
            "dest": _sanitize_for_log(f"s3://{DEST_BUCKET}/{dest_key}"),
            "size_bytes": len(content),
            "elapsed_ms": elapsed,
        },
    )

    # Emit per-file metrics via EMF
    category, subcategory = extract_category(dest_key)
    emit_emf("ProcessingTime", elapsed, "Milliseconds",
             {"Environment": ENVIRONMENT, "Category": category})
    emit_emf("FileSize", len(content), "Bytes",
             {"Environment": ENVIRONMENT, "Category": category})
    emit_emf("FilesProcessed", 1, "Count",
             {"Environment": ENVIRONMENT, "Category": category})
