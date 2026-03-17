
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, json, subprocess, sys
from pathlib import Path
try:
    import yaml
except Exception:
    yaml=None

def die(msg: str, code: int=1): print(f"ERROR: {msg}", file=sys.stderr); raise SystemExit(code)

def load_yaml(p: Path)->dict:
    if yaml is None: die('pyyaml required: pip3 install pyyaml')
    return yaml.safe_load(p.read_text()) or {}

def ssh_base(cfg: dict)->list[str]:
    return ['ssh','-i',cfg['ssh_private_key_path'],'-p',str(cfg.get('ssh_port',22)),'-o','StrictHostKeyChecking=no',f"{cfg['ssh_user']}@{cfg['host']}"]

def ssh_run(cfg: dict, cmd: str):
    full=ssh_base(cfg)+[cmd]; print('+',' '.join(full))
    return subprocess.run(full,text=True,capture_output=True)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',required=True); ap.add_argument('--state',required=True); args=ap.parse_args()
    cfg=load_yaml(Path(args.config))['proxmox']; sj=Path(args.state)/'started_vmids.json'
    if not sj.exists(): die(f"State file not found: {sj}")
    vmids=json.loads(sj.read_text()).get('vmids',[])
    print('=== qm list ==='); print(ssh_run(cfg,'qm list').stdout)
    for vmid in vmids:
        vmid=int(vmid); print(f"=== qm status {vmid} ==="); print(ssh_run(cfg,f"qm status {vmid}").stdout.strip())
if __name__=='__main__': main()
