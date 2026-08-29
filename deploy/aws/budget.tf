# ---------------------------------------------------------------------------
# Spend guardrail.
#
# AWS provides two budgets at no charge. On a free-tier or credit-funded
# account this is the single highest-value thing in the stack per line of
# code: it is the difference between noticing overspend on day two and
# noticing it when the credits are gone.
#
# IMPORTANT: a budget ALERTS. It does not CAP. Nothing here stops spend; it
# only tells you it is happening. There is no AWS setting that hard-stops
# billing, which is the most common and most expensive misunderstanding
# people have about the free tier.
# ---------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  count = var.monthly_budget_usd > 0 ? 1 : 0

  name         = "${local.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  lifecycle {
    precondition {
      condition     = var.budget_alert_email != ""
      error_message = "budget_alert_email must be set when monthly_budget_usd > 0. Set monthly_budget_usd = 0 to skip budget creation entirely (not recommended on a free-tier account)."
    }
  }

  # Early warning: half the budget consumed.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  # Act-now warning.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  # Forecast-based: fires when AWS projects month-end spend will exceed the
  # budget, which on a bulk ingestion run arrives days before actual spend
  # does. This is the alert that gives you time to react.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
# Made by Ryan Gomez & Co. Inc.
