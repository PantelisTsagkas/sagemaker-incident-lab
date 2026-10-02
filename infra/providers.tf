provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project     = "incident-lab"
      environment = "lab"
      managed-by  = "terraform"
    }
  }
}   