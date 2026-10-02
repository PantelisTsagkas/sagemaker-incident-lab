variable "region" {
  type        = string
  description = "AWS region for all lab resources"
  default     = "eu-west-2"
}

variable "alert_email" {
  type        = string
  description = "Where budgets alerts will be sent"
}

variable "budget_limit_usd" {
  type        = number
  description = "The budget limit in USD"
  default     = 20
}
