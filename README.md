# voloslotalerts

Lambda function that polls Volo's unauthenticated GraphQL endpoint for open volleyball pickup or drop-in slots at DU Gates Fieldhouse and Club Volo SoBo Indoor. When new slots appear, it sends an SMS via SNS and records event keys in DynamoDB to avoid duplicate notifications.

## Files
- `lambda_function.py` – AWS Lambda handler containing the polling, DynamoDB de-dupe, and SNS notification logic.

## Environment variables
- `SNS_TOPIC_ARN` – ARN for an SNS topic that has the desired phone numbers subscribed.
- `DDB_TABLE_NAME` – DynamoDB table name containing a string primary key `EventKey` (and optional `CreatedAt`).

## Notes
- Uses the DiscoverDaily query against `https://volosports.com/hapi/v1/graphql` with the `PLAYER` role header.
- Only considers volleyball pickup programs in Denver at the indoor venues listed in `VENUE_IDS`.
- Alerts only when new game or league entries with available spots are detected.
