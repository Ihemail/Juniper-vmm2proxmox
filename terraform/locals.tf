locals {
  cfg = yamldecode(file(var.config_yaml)).proxmox
}