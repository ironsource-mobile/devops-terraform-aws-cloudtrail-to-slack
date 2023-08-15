#module "cloudtrail_to_slack_dynamodb_table" {
#  count   = var.slack_bot_token != null ? 1 : 0
#  source  = "terraform-aws-modules/dynamodb-table/aws"
#  version = "3.3.0"
#  name    = var.dynamodb_table_name
#
#  hash_key           = "principal_structure_and_action_hash"
#  ttl_attribute_name = "ttl"
#  ttl_enabled        = true
#
#  attributes = [
#    {
#      name = "principal_structure_and_action_hash"
#      type = "S"
#    },
#  ]
#  tags = var.tags
#
#}

resource "aws_dynamodb_table" "cloudtrail_to_slack" {
  count        = var.slack_bot_token != null ? 1 : 0
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST" # on demand billing
  hash_key     = "principal_structure_and_action_hash"
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
  attribute {
    name = "principal_structure_and_action_hash"
    type = "S"
  }
  tags = var.tags
}