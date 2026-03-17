# Terraform Debugging

To enable verbose Terraform logging for this bundle:

1. In PowerShell (from the `terraform` directory or workspace root), set the standard Terraform debug variables before running `terraform` commands:

```powershell
$env:TF_LOG = "DEBUG"
$env:TF_LOG_PATH = "$PSScriptRoot/terraform-debug.log"
```

2. Run your Terraform command (for example `terraform apply`). Terraform writes detailed CLI logs to `terraform/terraform-debug.log` while summaries still appear in the console.

3. When you no longer need the debug output, clear the environment variables so subsequent runs return to normal verbosity:

```powershell
Remove-Item Env:TF_LOG
Remove-Item Env:TF_LOG_PATH
```

The generated log file remains in this folder (`terraform/terraform-debug.log`) for later inspection or sharing.
