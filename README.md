# voloslotalerts

Lambda function that polls Volo's unauthenticated GraphQL endpoint for open volleyball pickup or drop-in slots at DU Gates Fieldhouse, Club Volo SoBo Indoor, and Volo Sports Arena. When new slots appear, it sends an SMS via SNS and records event keys in DynamoDB to avoid duplicate notifications.

## Architecture overview

<img width="3598" height="928" alt="infrastructure-composer-template yaml" src="https://github.com/user-attachments/assets/089859ca-4914-4070-aece-e1a60f31946b" />

## Files
- `lambda_function.py` – AWS Lambda handler containing the polling, DynamoDB de-dupe, and SNS notification logic.

## Environment variables
- `SNS_TOPIC_ARN` – ARN for an SNS topic that has the desired phone numbers subscribed.
- `DDB_TABLE_NAME` – DynamoDB table name containing a string partition key `EventKey` (no sort key).
- `LOCAL_TIMEZONE` – Optional IANA timezone name used to format alerts (defaults to `America/Denver`).

## Required AWS permissions
The Lambda execution role needs permission to publish to the SNS topic and read/write to the DynamoDB table:

- SNS: `sns:Publish` on the topic referenced by `SNS_TOPIC_ARN`.
- DynamoDB: `dynamodb:GetItem`, `dynamodb:BatchGetItem`, and `dynamodb:PutItem` on the table referenced by `DDB_TABLE_NAME`.

Granting only these actions (and scoping them to the specific resources) keeps the role minimal while allowing alerts and de-duplication to function. Example IAM policy JSON for the Lambda execution role:

```
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sns:Publish"],
      "Resource": ["${SNS_TOPIC_ARN}"]
    },
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:BatchGetItem",
        "dynamodb:PutItem"
      ],
      "Resource": ["arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${DDB_TABLE_NAME}"]
    }
  ]
}
```


## Connectivity probe (local + Lambda)
If you want to quickly confirm whether the runtime can reach Volo's GraphQL endpoint (and whether you're being blocked with something like `error code: 1010`), use `connectivity_probe.py`.

- **Local run**

  ```bash
  python connectivity_probe.py
  ```

- **Lambda run**
  - Zip/upload `connectivity_probe.py` as your Lambda code (or include it in your package).
  - Set the handler to: `connectivity_probe.lambda_handler`
  - Invoke with any test event payload.
  - Inspect the returned fields:
    - `ok`
    - `http_status`
    - `body_preview` (where `error code: 1010` usually appears if blocked)
    - `response_headers`
    - `diagnosis` (classification + suggested next step)

Optional env vars for the probe:
- `PROBE_ENDPOINT` (default: `https://volosports.com/hapi/v1/graphql`)
- `PROBE_TIMEOUT_SECONDS` (default: `20`)
- `PROBE_USER_AGENT` (default: empty)
- `PROBE_MODE` (default: `minimal`; use `discover` to run the DiscoverDaily-style query + filters used by the Lambda)

## Notes
- Uses the DiscoverDaily query against `https://volosports.com/hapi/v1/graphql` with the `PLAYER` role header.
- Only considers volleyball pickup programs in Denver at the indoor venues listed in `VENUE_IDS` (DU Gates Fieldhouse, Club Volo SoBo Indoor, and Volo Sports Arena).
- Alerts only when new game or league entries with available spots are detected.

## DynamoDB table schema
The DynamoDB table must use **only a single string partition key named `EventKey`** and **no sort key**. Do not provision a composite key. Example table creation commands:

- **AWS CLI**

  ```bash
  aws dynamodb create-table \
    --table-name "$DDB_TABLE_NAME" \
    --attribute-definitions AttributeName=EventKey,AttributeType=S \
    --key-schema AttributeName=EventKey,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST
  ```

- **Terraform**

  ```hcl
  resource "aws_dynamodb_table" "events" {
    name         = var.ddb_table_name
    billing_mode = "PAY_PER_REQUEST"

    hash_key = "EventKey"

    attribute {
      name = "EventKey"
      type = "S"
    }
  }
  ```

- **CloudFormation (YAML)**

  ```yaml
  Resources:
    EventsTable:
      Type: AWS::DynamoDB::Table
      Properties:
        TableName: !Ref DDBTableName
        BillingMode: PAY_PER_REQUEST
        AttributeDefinitions:
          - AttributeName: EventKey
            AttributeType: S
        KeySchema:
          - AttributeName: EventKey
            KeyType: HASH
  Parameters:
    DDBTableName:
      Type: String
  ```
