locals {
  conf_dir = "${var.output_dir}/proxmox/qemu-server"
  plan     = "${var.output_dir}/plan/attach_plan.json"
  conf_files = fileset(local.conf_dir, "*.conf")
  conf_hash  = sha256(join("", [for f in local.conf_files : filesha256("${local.conf_dir}/${f}")]))
  plan_hash  = filesha256(local.plan)
}

resource "null_resource" "pre_apply_setup" {
  count = var.auto_pre_apply_setup ? 1 : 0

  triggers = {
    vmm_hash             = try(filesha256(var.vmm_file), "")
    plan_hash            = try(filesha256(local.plan), "")
    config_hash          = try(filesha256(var.config_yaml), "")
    type_registry_hash   = try(filesha256("${var.bundle_dir}/type_registry.yaml"), "")
    overrides_hash       = try(filesha256("${var.bundle_dir}/overrides.yaml"), "")
    image_selection_hash = try(filesha256(var.image_selection_file), "")
  }
  provisioner "local-exec" {
    command = join(" ", [
      "python",
      "../scripts/pre_apply_setup.py",
      "--config", var.config_yaml,
      "--plan", local.plan,
      "--type-registry", "${var.bundle_dir}/type_registry.yaml",
      "--overrides", "${var.bundle_dir}/overrides.yaml",
      "--vmm-file", var.vmm_file,
      "--state-dir", "${var.bundle_dir}/state",
      "--selection-file", var.image_selection_file
    ])
  }
}

resource "null_resource" "ensure_qmconf_dirs" {
  depends_on = [null_resource.pre_apply_setup]
  triggers   = { node_name = local.cfg.node_name }

  connection {
    type        = "ssh"
    host        = local.cfg.host
    user        = local.cfg.ssh_user
    port        = local.cfg.ssh_port
    private_key = file(local.cfg.ssh_private_key_path)
  }

  provisioner "remote-exec" {
    inline = [
      "test -d /etc/pve/qemu-server || mkdir -p /etc/pve/qemu-server",
      "test -d /etc/pve/nodes/${local.cfg.node_name}/qemu-server || mkdir -p /etc/pve/nodes/${local.cfg.node_name}/qemu-server"
    ]
  }
}

resource "null_resource" "copy_qmconf" {
  depends_on = [null_resource.ensure_qmconf_dirs]
  for_each   = { for f in local.conf_files : f => f }
  triggers   = { conf_hash = local.conf_hash, file_name = each.key }

  connection {
    type        = "ssh"
    host        = local.cfg.host
    user        = local.cfg.ssh_user
    port        = local.cfg.ssh_port
    private_key = file(local.cfg.ssh_private_key_path)
  }

  provisioner "file" {
    source      = "${local.conf_dir}/${each.key}"
    destination = "/etc/pve/nodes/${local.cfg.node_name}/qemu-server/${each.key}"
  }
}

resource "null_resource" "copy_qmconf_shared" {
  depends_on = [null_resource.ensure_qmconf_dirs]
  for_each   = { for f in local.conf_files : f => f }
  triggers   = { conf_hash = local.conf_hash, file_name = each.key }

  connection {
    type        = "ssh"
    host        = local.cfg.host
    user        = local.cfg.ssh_user
    port        = local.cfg.ssh_port
    private_key = file(local.cfg.ssh_private_key_path)
  }

  provisioner "file" {
    source      = "${local.conf_dir}/${each.key}"
    destination = "/etc/pve/qemu-server/${each.key}"
  }
}

resource "null_resource" "copy_qmconf_done" {
  depends_on = [null_resource.copy_qmconf, null_resource.copy_qmconf_shared]
  triggers = {
    conf_files = jsonencode(sort(keys(null_resource.copy_qmconf)))
    conf_hash  = local.conf_hash
  }
  provisioner "local-exec" {
    command = "echo '[INFO] All qm.conf files copied successfully'"
  }
}

resource "null_resource" "attach_and_start" {
  depends_on = [null_resource.copy_qmconf_done]
  triggers = {
    copy_files_list = jsonencode(sort(keys(null_resource.copy_qmconf)))
    conf_hash = local.conf_hash
    plan_hash = local.plan_hash
    interval  = tostring(var.start_interval_seconds)
    timeout   = tostring(var.start_timeout_seconds)
  }
  provisioner "local-exec" {
    command = join(" ", [
      "python", "../scripts/deploy_via_ssh.py",
      "--config", var.config_yaml,
      "--plan", local.plan,
      "--type-registry", "${var.bundle_dir}/type_registry.yaml",
      "--overrides", "${var.bundle_dir}/overrides.yaml",
      "--state-dir", "${var.bundle_dir}/state",
      "--start-interval", tostring(var.start_interval_seconds),
      "--start-timeout", tostring(var.start_timeout_seconds),
      "--image-selection", format("\"%s\"", var.image_selection_file)
    ])
  }
}

