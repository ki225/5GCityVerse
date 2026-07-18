terraform {
  # Partial configuration is supplied by scripts/deploy.sh after the isolated
  # bootstrap root creates the versioned state bucket.
  backend "s3" {}
}
