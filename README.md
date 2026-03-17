# Juniper-vmm2proxmox
Juniper's VMM config to Proxmox VM Conversion &amp; Deployer via terraform with all inter-connection bridges

# VMM Config to Proxmox Full Deploy Bundle (Terraform + Python)

## Overview
- Converts VMM config to Proxmox `qm.conf` files and an attach plan
- Creates `vmc_<first6-normalized>` inter-connection bridges automatically
- Copies configs, attaches disks/ISOs, starts VMs in order, and records started VMIDs
- On `terraform destroy`: shuts down recorded VMs and **prompts Yes/No (default No, 30s)** before deleting only created bridges

This README consolidates **all logic, knobs, VM specifications, bridge behavior, image handling, VLAN rules, CPU/RAM defaults**, and **operational flow**.

## Table of Contents
- [1. Repository Layout](#1-repository-layout)
- [2. Where to Configure Proxmox Server Details](#2-where-to-configure-proxmox-server-details)
- [3. Where to Place Default Images](#3-where-to-place-default-images)
- [4. VM Families, CPU & RAM Defaults](#4-vm-families-cpu--ram-defaults)
- [5. Bridge Logic](#5-bridge-logic)
- [6. VLAN Allocation Logic](#6-vlan-allocation-logic)
- [7. Image Resolution Logic](#7-image-resolution-logic)
- [8. Behavior Knobs (type_registry.yaml)](#8-behavior-knobs-type_registryyaml)
- [9. MAC Address Logic](#9-mac-address-logic)
- [10. Lifecycle](#10-lifecycle)
- [11. Notes](#11-notes)
- [12. Prerequisites](#12-prerequisites)
- [13. Usage](#13-usage)
	- [13.1 Convert](#131-convert)
	- [13.2 Apply](#132-apply-pre-flight-bridges--disk-selections-run-automatically)
	- [13.3 Status / Shutdown / Manual bridge delete](#133-status--shutdown--manual-bridge-delete)
	- [13.4 Manual Cleanup (after destroy)](#134-manual-cleanup-after-destroy)
	- [13.5 Destroy](#135-destroy)
- [14. Helper Scripts - Ubuntu](#14-helper-scripts---ubuntu)
- [15. Helper Scripts - Windows (`scripts/windows`)](#15-helper-scripts---windowsscriptswindows)
	- [15.1 Windows complete destroy + manual cleanup](#151-windows-complete-destroy--manual-cleanup)
- [16. Summary](#16-summary)
- [17. Extras](#17-extras)
	- [Windows 10/11 - Python 3 and Terraform Installation](#windows-1011---python-3-and-terraform-installation)

---
## 1. Repository Layout
```
.
├── config.yaml
├── type_registry.yaml
├── overrides.yaml
├── vmm_to_proxmox.py
├── scripts/
│   ├── create_bridges.py
│   ├── delete_bridges.py
│   ├── deploy_via_ssh.py
│   ├── shutdown_all.py
│   ├── status_all.py
│   └── helpers...
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── locals.tf...
└── vmmconf_sample/
    ├── Master.conf
    ├── vModem.conf
    ├── vMX.conf
    ├── vPTX10k.conf
    ├── vQFX10k.conf
    ├── vSRX.conf
    └── Linux.conf...
```

---
## 2. Where to Configure Proxmox Server Details
Edit **config.yaml**:
```yaml
proxmox:
  host: "pve1.example.com"           # Proxmox IP/hostname
  ssh_user: "root"                   # Must be allowed to run qm + edit interfaces
  ssh_port: 22
  ssh_private_key_path: "/home/ubuntu/.ssh/id_ed25519"

  image_dir: "/root/import"         # Directory ON PROXMOX where QCOW2/VMDK/RAW/ISO are stored
  iso_storage_id: "local"
  iso_storage_path: "/var/lib/vz/template/iso"
```
All paths above are **on Proxmox**, not the runner.

---
## 3. Where to Place Default Images
Upload all images to Proxmox host under:
```
/root/import/
```
Examples:
- ubuntu-18.04.qcow2
- lede-disk0.qcow2
- junos-virtual-x86-64.vmdk
- cspp-ubn22.qcow2
- junos-evo-install.iso
- vqfx-re-15.1X53-D60.vmdk  *(vQFX RE)*
- vqfx10k-pfe-20.2R1.10.img  *(vQFX PFE)*
- media-vsrx-vmdisk.qcow2  *(vSRX)*

`overrides.yaml` maps VM types → default images.

---
## 4. VM Families, CPU & RAM Defaults
These values come from `type_registry.yaml`.

### 4.1 Linux VMs
```
Machine: q35
NIC Model: virtio
CPU Cores: 1
RAM: value from VMM file
Disk: IDE0
Boot: order=ide0
```

### 4.2 Modem (OpenWRT-like)
```
Machine: q35
NIC Model: virtio
CPU Cores: 1
RAM: from VMM
```

### 4.3 vMX Platform
#### vMX RE
```
Machine: pc-i440fx-7.0
Cores: 1
RAM: 1230 MB
NICs:
  net0 → mgmt
  net1 → fpc (VLAN applied)
IDE0: VMX_DISK (QCOW2/VMDK)
IDE1: extra_disk_ide1 (e.g., vmxhdd.qcow2)
Boot: order=ide0
```

### vMX MPC
```
Machine: pc-i440fx-7.0
Cores: 3
RAM: 2250 MB
NICs:
  net0 → mgmt
  net1 → fpc
  net2+ → GE interfaces from VMM
Disk: IDE0: VMX_DISK
```

## 4.4 vPTX Platform
### vPTX RE
```
Machine: pc-i440fx-7.0
Cores: 4
RAM: 4096 MB
NICs: fixed 5 NICs (mgmt + fpc with VLANs)
IDE0: extra_disk_ide0 (e.g., /root/import/vptxhdd.qcow2)
IDE2: ISO (junos-evo-install.iso)
Boot: order=ide0;ide2
```

### vPTX CSPP
```
Machine: pc-i440fx-7.0
Cores: 3
RAM: 3280 MB
NICs:
  net0..3 → fixed mgmt/fpc
  net4+   → IF_ET ports from VMM (e1000)
IDE0: CSPP QCOW2 image
Boot: order=ide0
```

## 4.5 vQFX Platform
### vQFX RE
```
Machine: pc-i440fx-7.0
Cores: 1
RAM: 1200 MB
NICs:
  net0 → mgmt
  net1 → internal link to vQFX PFE (VLAN applied)
  net2+ → GE/XE interfaces from VMM
IDE0: vQFX RE QCOW2/VMDK image (e.g., vqfx-re-15.1X53-D60.vmdk)
Boot: order=ide0
```

> **Important — vQFX RE Post-Boot Config:**
> Once the vQFX RE is online, login and apply the following config to bring the PFE online and vQFX interfaces up:
> ```
> set interfaces em1 unit 0 family inet address 169.254.0.2/24
> ```
> This establishes the internal RE↔PFE communication link over `em1` and is required before any data-plane interfaces become operational.

### vQFX PFE
```
Machine: pc-i440fx-7.0
Cores: 1
RAM: 1024 MB
NICs:
  net0 → mgmt
  net1 → internal link to vQFX RE (VLAN applied, must match RE net1 VLAN)
IDE0: vQFX PFE image (e.g., vqfx10k-pfe-20.2R1.10.img)
Boot: order=ide0
```

## 4.6 vSRX Platform
```
Machine: pc-i440fx-7.0
Cores: 2
RAM: 4096 MB
NIC Model: e1000
NICs:
  net0 → mgmt
  net1+ → ge-0/0/x interfaces from VMM
IDE0: vSRX QCOW2/VMDK image (e.g., media-vsrx-vmdisk.qcow2)
Boot: order=ide0
```

---
# 5. Bridge Logic
Two categories of bridges exist:

## 5.1 Infra Bridges (never created/deleted)
```
vmbr_mgmt
vmbr6_fpc
```

## 5.2 Inter‑connection Bridges (auto-managed)
All VMM bridges become:
```
vmc_<normalized>
```
Where normalization = lowercasing + remove all non-alphanumeric.

### Creation
`create_bridges.py`:
- Appends definitions inside `/etc/network/interfaces`
- Adds markers:
  
  `  # --- vmm2proxmox managed bridges (start)`
  
  `  # --- vmm2proxmox managed bridges (end)`
- Calls `ifreload -a`
- Records created bridges in `state/created_bridges.json`

### Deletion
Only removed if:
- During `terraform destroy`
- And you type **yes** within 30 seconds
- And bridge is listed in `created_bridges.json`

Safe-by-default: bridges remain unless explicitly deleted.

---
# 6. VLAN Allocation Logic
Defined in `type_registry.yaml`:
```yaml
vlan_policy:
  base: 1010
  step: 10
  order: [vmx, vptx, vqfx, vsrx]
```
### Computation
```
vmx-1  → VLAN 1010
vmx-2  → VLAN 1020
vptx-1 → VLAN 1030
vptx-2 → VLAN 1040
vqfx-1 → VLAN 1050
vqfx-2 → VLAN 1060
vsrx-1 → VLAN 1070
vsrx-2 → VLAN 1080
```

vPTX NIC tags:
```
vlan: base → VLAN N
vlan: base_plus_1 → VLAN N+1
```

---
# 7. Image Resolution Logic
Priority:
1. Token-based image from VMM `#define TOKEN basedisk "path"`
2. Override (overrides.yaml)
3. Default images
4. interactive=true → prompt
5. Skip or Fail depending on behavior

### Special: vPTX RE ISO
Checked in:
```
/var/lib/vz/template/iso/<iso>
```
Then attached at:
```
ide2: local:iso/<iso>
```
If not found and `interactive=true`, deploy prompts you to choose an ISO from `iso_storage_path`.

---
# 8. Behavior Knobs (type_registry.yaml)
```
interactive: false/true
unknown_action: skip/fail
vmid_start: 6000
storage_id: local-lvm
iso_storage_id: local
iso_storage_path: /var/lib/vz/template/iso
mac_prefix: BC:24:22
vlan_policy: {base, step, order}
vptx_cspp_mode: single/multi
```
These fully control deploy logic.

---
# 9. MAC Address Logic
Deterministic MACs:
```
<mac_prefix>:<3-byte SHA1 hash>
```
Ensures reproducibility across runs.

---
# 10. Lifecycle

1. Convert VMM to generated Proxmox artifacts in `output/`.
2. Run Terraform apply (includes automatic pre-flight bridge/image-selection checks).
3. Check status, operate workloads, and shut down when needed.
4. Run Terraform destroy; optionally confirm deletion of auto-created `vmc_*` bridges.

For exact, copy-paste commands, use the canonical section **# 13. Usage** below.

---
# 11. Notes
- All images must live **on Proxmox** under `/root/import`.
- vPTX RE `extra_disk_ide0` must exist on Proxmox (no blank ide0 fallback).
- vPTX RE ISO is resolved from `iso_storage_path` and attached as CDROM on `ide2`.
- **vQFX RE** requires a two-VM pair: one RE VM and one PFE VM. The `net1` NIC on both must share the same internal VLAN bridge to establish the RE↔PFE link.
- **vQFX RE post-boot:** After the RE comes online, apply the following config to bring the PFE online and all data-plane interfaces up:
  ```
  set interfaces em1.0 family inet address 169.254.0.2/24
  ```
- **vQFX PFE** image is a raw/QCOW2 appliance image; no additional disk or ISO needed.
- **vSRX** boots from a single QCOW2/VMDK image; all interfaces are `e1000` by default. Ensure adequate RAM (≥4 GB) for stable operation.
- vmbr_mgmt & vmbr6_fpc never created/deleted.
- VMIDs start from 6000 by default.
- All VMs have `serial0: socket`.
- All disks attach via IDE as per your preference.

---
# 12. Prerequisites
Before running convert/apply/destroy commands, ensure:

- **Python 3** is installed and available as `python3` (Linux) or `python` / `py -3` (Windows)
- **Terraform** is installed and available in your PATH
- Required Python packages are installed: `paramiko`, `pyyaml`

Install references:
- Windows 10/11 install scripts and links: see [Windows 10/11 - Python 3 and Terraform Installation](#windows-1011---python-3-and-terraform-installation)
- Ubuntu install scripts and links: see [Python 3 Installation on Ubuntu (Latest Version)](#python-3-installation-on-ubuntu-latest-version) and [Terraform Installation on Ubuntu](#terraform-installation-on-ubuntu)

Quick preflight checks:

```powershell
python --version
py -3 --version
terraform --version
python -m pip show paramiko pyyaml
```

---
# 13. Usage
## 13.1 Convert
```bash
python3 vmm_to_proxmox.py --vmm ./input.vmm --type-registry ./type_registry.yaml --overrides ./overrides.yaml --out ./output
```

## 13.2 Apply (pre-flight bridges + disk selections run automatically)
```bash
cd terraform
terraform init
terraform apply -auto-approve \
	-var output_dir="../output" \
	-var config_yaml="../config.yaml" \
	-var vmm_file="../output/input.vmm" \
	-var start_interval_seconds=30 \
	-var start_timeout_seconds=600 \
	-var image_selection_file="../state/selected_images.json"
```

> **Automatic Pre-flight Setup:** `terraform apply` automatically runs `scripts/pre_apply_setup.py` at the beginning to create any missing bridges and collect disk-image selections. All CLI logs from this pre-flight step will be visible in the Terraform output.

## 13.3 Status / Shutdown / Manual bridge delete
```bash
./scripts/status_all.sh           # Show VM and bridge status
./scripts/shutdown_all.sh         # Shutdown all VMs
./scripts/delete_bridges.sh       # Delete all created bridges (manual prompt)
```

## 13.4 Manual Cleanup (after destroy)
```bash
cd scripts
./manual_cleanup.sh               # Cleans up VMs and bridges after terraform destroy
```

## 13.5 Destroy
```bash
cd terraform
terraform destroy -auto-approve   -var output_dir="../output"   -var config_yaml="../config.yaml"   -var vmm_file="../output/input.vmm"
```

# 14. Helper Scripts - Ubuntu
All helper scripts are in the `scripts/` folder:

- create_bridges.py         - Create Proxmox bridges from VMM config  
	```bash
	python3 scripts/create_bridges.py --config ./config.yaml --vmm-file ./output/input.vmm --state-dir ./state
	# optional: skip ifreload -a
	python3 scripts/create_bridges.py --config ./config.yaml --vmm-file ./output/input.vmm --state-dir ./state --no-apply
	```

- pre_apply_setup.py        - Interactive pre-flight (bridges + missing image selections)  
	```bash
	python3 scripts/pre_apply_setup.py --config ./config.yaml --plan ./output/plan/attach_plan.json --type-registry ./type_registry.yaml --overrides ./overrides.yaml --vmm-file ./output/input.vmm --state-dir ./state --selection-file ./state/selected_images.json
	# optional: skip bridge creation and only do image-selection checks
	python3 scripts/pre_apply_setup.py --config ./config.yaml --plan ./output/plan/attach_plan.json --type-registry ./type_registry.yaml --overrides ./overrides.yaml --state-dir ./state --selection-file ./state/selected_images.json --skip-bridges
	```

- delete_bridges.py         - Delete bridges (Python)  
	```bash
	python3 scripts/delete_bridges.py --config ./config.yaml --state-dir ./state
	# optional: force delete even if bridge has member ports
	python3 scripts/delete_bridges.py --config ./config.yaml --state-dir ./state --force
	```

- delete_bridges.sh         - Delete bridges (Shell wrapper)  
	```bash
	./scripts/delete_bridges.sh
	# pass-through flags are supported
	./scripts/delete_bridges.sh --force --no-apply
	```

- deploy_via_ssh.py         - Deploy VMs via SSH  
	```bash
	python3 scripts/deploy_via_ssh.py --config ./config.yaml --plan ./output/plan/attach_plan.json --type-registry ./type_registry.yaml --overrides ./overrides.yaml --state-dir ./state --start-interval 30 --start-timeout 600 --image-selection ./state/selected_images.json
	```

- generate_drawio_xml.py    - Generate draw.io XML from VMM  
	```bash
	python3 scripts/generate_drawio_xml.py --vmm-file ./output/input.vmm --out ./output/reports/topology.drawio.xml
	# optional positional input form
	python3 scripts/generate_drawio_xml.py ./output/input.vmm --out ./output/reports/topology.drawio.xml
	```

- manual_cleanup.sh         - Manual cleanup for VMs and bridges (run after destroy)  
	```bash
	./scripts/manual_cleanup.sh
	```

- shutdown_all.py           - Shutdown all VMs (Python)  
	```bash
	python3 scripts/shutdown_all.py --config ./config.yaml --state ./state
	# optional force stop after timeout
	python3 scripts/shutdown_all.py --config ./config.yaml --state ./state --timeout 600 --interval 10 --force
	```

- shutdown_all.sh           - Shutdown all VMs (Shell wrapper)  
	```bash
	./scripts/shutdown_all.sh
	# pass-through flags are supported
	./scripts/shutdown_all.sh --force --timeout 300
	```

- status_all.py             - Show status of all VMs (Python)  
	```bash
	python3 scripts/status_all.py --config ./config.yaml --state ./state
	```

- status_all.sh             - Show status of all VMs (Shell wrapper)  
	```bash
	./scripts/status_all.sh
	```

# 15. Helper Scripts - Windows (`scripts/windows`)
Run the Windows copies in `scripts/windows` directly with Python (no OpenSSH/`ssh.exe` required):

```powershell
# install Python dependencies once
python -m pip install paramiko pyyaml
# or
py -3 -m pip install paramiko pyyaml
```

```powershell
# pre-flight setup (bridges + image selections)
python .\scripts\windows\pre_apply_setup.py --config .\config.yaml --plan .\output\plan\attach_plan.json --type-registry .\type_registry.yaml --overrides .\overrides.yaml --vmm-file .\output\input.vmm --state-dir .\state --selection-file .\state\selected_images.json
# or
py -3 .\scripts\windows\pre_apply_setup.py --config .\config.yaml --plan .\output\plan\attach_plan.json --type-registry .\type_registry.yaml --overrides .\overrides.yaml --vmm-file .\output\input.vmm --state-dir .\state --selection-file .\state\selected_images.json

# deploy/attach/start
python .\scripts\windows\deploy_via_ssh.py --config .\config.yaml --plan .\output\plan\attach_plan.json --type-registry .\type_registry.yaml --overrides .\overrides.yaml --state-dir .\state --start-interval 30 --start-timeout 600 --image-selection .\state\selected_images.json
# or
py -3 .\scripts\windows\deploy_via_ssh.py --config .\config.yaml --plan .\output\plan\attach_plan.json --type-registry .\type_registry.yaml --overrides .\overrides.yaml --state-dir .\state --start-interval 30 --start-timeout 600 --image-selection .\state\selected_images.json

# status
python .\scripts\windows\status_all.py --config .\config.yaml --state .\state
# or
py -3 .\scripts\windows\status_all.py --config .\config.yaml --state .\state

# shutdown (optional args)
python .\scripts\windows\shutdown_all.py --config .\config.yaml --state .\state --timeout 300 --force
# or
py -3 .\scripts\windows\shutdown_all.py --config .\config.yaml --state .\state --timeout 300 --force

# delete created bridges (optional args)
python .\scripts\windows\delete_bridges.py --config .\config.yaml --state-dir .\state --force --no-apply
# or
py -3 .\scripts\windows\delete_bridges.py --config .\config.yaml --state-dir .\state --force --no-apply

# create bridges only
python .\scripts\windows\create_bridges.py --config .\config.yaml --vmm-file .\output\input.vmm --state-dir .\state
# or
py -3 .\scripts\windows\create_bridges.py --config .\config.yaml --vmm-file .\output\input.vmm --state-dir .\state

## 15.1 Windows complete destroy + manual cleanup

```powershell
# 1) Terraform destroy (from repo root)
Set-Location .\terraform
terraform destroy -auto-approve -var output_dir="../output" -var config_yaml="../config.yaml" -var vmm_file="../output/input.vmm"

# 2) Return to repo root
Set-Location ..

# 3) Ensure all recorded VMs are stopped (force stop fallback)
python .\scripts\windows\shutdown_all.py --config .\config.yaml --state .\state --timeout 300 --interval 10 --force
# or
py -3 .\scripts\windows\shutdown_all.py --config .\config.yaml --state .\state --timeout 300 --interval 10 --force

# 4) Delete created vmc_* bridges and apply network reload
python .\scripts\windows\delete_bridges.py --config .\config.yaml --state-dir .\state --force
# or
py -3 .\scripts\windows\delete_bridges.py --config .\config.yaml --state-dir .\state --force

# 5) Verify cleanup state
python .\scripts\windows\status_all.py --config .\config.yaml --state .\state
# or
py -3 .\scripts\windows\status_all.py --config .\config.yaml --state .\state
```

Optional interactive cleanup wrapper:

```powershell
.\scripts\windows\manual_cleanup.ps1
```

Optional: you can still use the PowerShell wrappers in `scripts/windows/*.ps1`, but direct Python execution is the recommended Windows path.

# 16. Summary
This bundle provides a deterministic, safe, reproducible automation pipeline for deploying complex multi-VM Juniper-based labs on Proxmox—including bridge creation, VLAN wiring, image handling, VM boot sequencing, and stateful teardown.

# 17. Extras

## Windows 10/11 - Python 3 and Terraform Installation

### 📌 Official Windows install links
- Python 3 (Windows): https://www.python.org/downloads/windows/
- Terraform install docs (Windows): https://developer.hashicorp.com/terraform/install#windows

### Install using winget (recommended)
```powershell
# Python 3
winget install -e --id Python.Python.3.12

# Terraform
winget install -e --id Hashicorp.Terraform

# Verify
python --version
py -3 --version
terraform --version
```

### Install using Chocolatey (alternative)
```powershell
# Python 3
choco install -y python

# Terraform
choco install -y terraform

# Verify
python --version
terraform --version
```

### Configure pip dependencies for this project (Windows)
```powershell
python -m pip install --upgrade pip
python -m pip install paramiko pyyaml
```

## Python 3 Installation on Ubuntu (Latest Version)

### 📌 Official Python Downloads  
You can always download the latest Python releases from the official Python.org website:  
👉 https://www.python.org/downloads/

---

## Installing Python 3 on Ubuntu

### **1. Update system packages**
```
sudo apt update
```

### **2. Install Python and set `python` → `python3`**
```
sudo apt install -y python-is-python3
```

### **3. Install Paramiko**
```
sudo python -m pip install paramiko
```

### **4. Install networking tools**
```
sudo apt install -y iproute2 bridge-utils net-tools
sudo modprobe bridge
```

---

# Terraform Installation on Ubuntu
Terraform can be installed using the official HashiCorp APT repository.

### 📌 Official Terraform Installation Page
👉 https://developer.hashicorp.com/terraform/install

### **1. Install dependencies**
```
sudo apt update
sudo apt install -y gnupg software-properties-common curl
```

### **2. Add HashiCorp GPG key**
```
wget -O- https://apt.releases.hashicorp.com/gpg |   gpg --dearmor | sudo tee /usr/share/keyrings/hashicorp-archive-keyring.gpg > /dev/null
```

### **3. Add HashiCorp APT repository**
```
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" |   sudo tee /etc/apt/sources.list.d/hashicorp.list
```

### **4. Install Terraform**
```
sudo apt update
sudo apt install terraform
```

### **5. Verify installation**
```
terraform --version
```

