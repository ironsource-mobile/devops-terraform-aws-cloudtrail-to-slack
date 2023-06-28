# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at

#   http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import gzip
import json

import os
import urllib

from typing import Any, Dict, List, NamedTuple

import boto3
from config import Config, get_logger

from slack_helpers import (
    post_message,
    message_for_slack_error_notification,
    event_to_slack_message,
    message_for_rule_evaluation_error_notification
)


cfg = Config()
logger = get_logger()


def lambda_handler(s3_notification_event: Dict[str, List[Any]], _) -> int:  # noqa: ANN001

    try:
        for record in s3_notification_event["Records"]:
            event_name: str = record["eventName"]
            if "Digest" not in record["s3"]["object"]["key"]:
                logger.debug({"s3_notification_event": s3_notification_event})

            if event_name.startswith("ObjectRemoved"):
                handle_removed_object_record(
                    record = record,
                )
                continue

            elif event_name.startswith("ObjectCreated"):
                handle_created_object_record(
                    record = record,
                    cfg = cfg,
                )
                continue

    except Exception as e:
        post_message(
            message = message_for_slack_error_notification(e, s3_notification_event),
            account_id = None
        )
        logger.exception({"Failed to process event": e})
    return 200


def handle_removed_object_record(
        record: dict,
) -> None:
    logger.info({"s3:ObjectRemoved event": record})
    account_id = record["userIdentity"]["accountId"] if "accountId" in record["userIdentity"] else ""
    message = event_to_slack_message(
        event = record,
        source_file = record["s3"]["object"]["key"],
        account_id_from_event = account_id,
    )
    post_message(message = message, account_id = account_id)


def handle_created_object_record(
        record: dict,
        cfg: Config,
) -> None:
    cloudtrail_log_record = get_cloudtrail_log_records(record)
    if cloudtrail_log_record:
        for cloudtrail_log_event in cloudtrail_log_record["events"]:
            handle_event(
                event = cloudtrail_log_event,
                source_file_object_key = cloudtrail_log_record["key"],
                rules = cfg.rules,
                ignore_rules = cfg.ignore_rules
            )


def get_cloudtrail_log_records(record: Dict) -> Dict | None:
    # Get all the files from S3 so we can process them
    s3 = boto3.client("s3")

    # In case if we get something unexpected
    if "s3" not in record:
        raise AssertionError(f"recieved record does not contain s3 section: {record}")
    bucket = record["s3"]["bucket"]["name"]
    key = urllib.parse.unquote_plus(record["s3"]["object"]["key"], encoding="utf-8") # type: ignore # noqa: PGH003, E501
    # Do not process digest files
    if "Digest" in key:
        return
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        with gzip.GzipFile(fileobj=response["Body"]) as gzipfile:
            content = gzipfile.read()
        content_as_json = json.loads(content.decode("utf8"))
        cloudtrail_log_record = {
            "key": key,
            "events": content_as_json["Records"],
        }

    except Exception as e:
        logger.exception({"Error getting object": {"key": key, "bucket": bucket, "error": e}})
        raise e
    return cloudtrail_log_record


class ProcessingResult(NamedTuple):
    should_be_processed: bool
    errors: List[Dict[str, Any]]


def should_message_be_processed(
    event: Dict[str, Any],
    rules: List[str],
    ignore_rules: List[str],
) -> ProcessingResult:
    flat_event = flatten_json(event)
    user = event["userIdentity"]
    event_name = event["eventName"]
    logger.debug({"Rules:": rules, "ignore_rules": ignore_rules})
    logger.debug({"Flattened event": flat_event})

    errors = []
    for ignore_rule in ignore_rules:
        try:
            if eval(ignore_rule, {}, {"event": flat_event}) is True: # noqa: PGH001
                logger.info({"Event matched ignore rule and will not be processed": {"ignore_rule": ignore_rule, "flat_event": flat_event}}) # noqa: E501
                return ProcessingResult(False, errors)
        except Exception as e:
            logger.exception({"Event parsing failed": {"error": e, "ignore_rule": ignore_rule, "flat_event": flat_event}}) # noqa: E501
            errors.append({"error": e, "rule": ignore_rule})

    for rule in rules:
        try:
            if eval(rule, {}, {"event": flat_event}) is True: # noqa: PGH001
                logger.info({"Event matched rule and will be processed": {"rule": rule, "flat_event": flat_event}}) # noqa: E501
                return ProcessingResult(True, errors)
        except Exception as e:
            logger.exception({"Event parsing failed": {"error": e, "rule": rule, "flat_event": flat_event}})
            errors.append({"error": e, "rule": rule})

    logger.info({"Event did not match any rules and will not be processed": {"event": event_name, "user": user}}) # noqa: E501
    return ProcessingResult(False, errors)


def handle_event(
    event: Dict[str, Any],
    source_file_object_key: str,
    rules: List[str],
    ignore_rules: List[str],
) -> None:

    result = should_message_be_processed(event, rules, ignore_rules)
    account_id = event["userIdentity"]["accountId"] if "accountId" in event["userIdentity"] else""
    if cfg.rule_evaluation_errors_to_slack:
        for error in result.errors:
            post_message(
                message = message_for_rule_evaluation_error_notification(
                error = error["error"],
                object_key = source_file_object_key,
                rule = error["rule"],
                ),
                account_id = account_id,
            )

    if not result.should_be_processed:
        return

    # log full event if it is AccessDenied
    if ("errorCode" in event and "AccessDenied" in event["errorCode"]):
        event_as_string = json.dumps(event, indent=4)
        logger.info({"errorCode": "AccessDenied", "log full event": event_as_string})
    message = event_to_slack_message(event, source_file_object_key, account_id)
    post_message(message = message, account_id = account_id,)


# Flatten json
def flatten_json(y: dict) -> dict:
    out = {}

    def flatten(x, name=""): # noqa: ANN001, ANN202
        if type(x) is dict:
            for a in x:
                flatten(x[a], name + a + ".")
        elif type(x) is list:
            i = 0
            for a in x:
                flatten(a, name + str(i) + ".")
                i += 1
        else:
            out[name[:-1]] = x

    flatten(y)
    return out

# For local testing
if __name__ == "__main__":
    from rules import default_rules
    hook_url = os.environ.get("HOOK_URL")
    if hook_url is None:
        raise Exception("HOOK_URL is not set!")
    ignore_rules = ["'userIdentity.accountId' in event and event['userIdentity.accountId'] == 'YYYYYYYYYYY'"]
    with open("./tests/test_events.json") as f:
        data = json.load(f)
    for event in data:
        handle_event(event, "file_name", default_rules, ignore_rules)
