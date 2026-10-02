data "aws_iam_policy_document" "sagemaker_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    effect  = "Allow"
    principals {
      type        = "Service"
      identifiers = ["sagemaker.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sagemaker_execution" {
  name               = "incident-lab-sagemaker-execution"
  assume_role_policy = data.aws_iam_policy_document.sagemaker_trust.json
}

# What the endpoint may do once SageMaker has assumed the role.
# Written by hand because AmazonSageMakerFullAccess only grants S3 read on
# buckets with "sagemaker" in the name, and ours is incident-lab-<account>.
data "aws_iam_policy_document" "sagemaker_permissions" {
  # Download model.tar.gz at endpoint startup.
  statement {
    sid       = "ReadModelArtifacts"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }

  # ECR login. This action does not support resource-level scoping.
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Pull the AWS-owned scikit-learn serving image (account 764974769150 in eu-west-2).
  statement {
    sid    = "PullServingImage"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchCheckLayerAvailability",
    ]
    resources = ["arn:aws:ecr:${var.region}:764974769150:repository/sagemaker-scikit-learn"]
  }

  # Container stdout to /aws/sagemaker/Endpoints/<name>, plus custom metrics.
  statement {
    sid    = "WriteLogsAndMetrics"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
      "cloudwatch:PutMetricData",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "sagemaker_execution" {
  name   = "incident-lab-execution"
  role   = aws_iam_role.sagemaker_execution.id
  policy = data.aws_iam_policy_document.sagemaker_permissions.json
}

