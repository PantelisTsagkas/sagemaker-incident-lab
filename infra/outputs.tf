output "bucket_name" {
  value = aws_s3_bucket.artifacts.bucket
}

output "role_arn" {
  value = aws_iam_role.sagemaker_execution.arn
}

