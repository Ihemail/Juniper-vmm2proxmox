variable "config_yaml" {
  type    = string
  default = "../config.yaml"
}
variable "output_dir" {
  type    = string
  default = "../output"
}
variable "bundle_dir" {
  type    = string
  default = ".."
}
variable "vmm_file" {
  description = "Path to the VMM file"
  type        = string
  default     = "../output/input.vmm"
}
variable "start_interval_seconds" {
  type    = number
  default = 30
}
variable "start_timeout_seconds" {
  type    = number
  default = 180
}
variable "ssh_private_key_path" {
  description = "Absolute path to the SSH private key used for remote operations."
  type        = string
}
variable "image_selection_file" {
  description = "Path to the JSON file containing pre-selected VM image mappings."
  type        = string
  default     = "../state/selected_images.json"
}

variable "auto_pre_apply_setup" {
  description = "Run scripts/pre_apply_setup.py automatically from Terraform local-exec. Disable for cleaner manual interactive selection flow."
  type        = bool
  default     = true
}
