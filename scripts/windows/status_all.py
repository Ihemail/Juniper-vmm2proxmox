
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
try:
    import yaml
except Exception:
    yaml=None
try:
    import paramiko
except Exception:
    paramiko=None

def die(msg: str, code: int=1): print(f"ERROR: {msg}", file=sys.stderr); raise SystemExit(code)

def load_yaml(p: Path)->dict:
    if yaml is None: die('pyyaml required: pip3 install pyyaml')
    return yaml.safe_load(p.read_text()) or {}

def ssh_run(cfg: dict, cmd: str, check: bool=False):
    if paramiko is None:
        die('paramiko required: pip install paramiko')
    print(f"+ ssh {cfg['host']}: {cmd}")
    client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(cfg['host'], port=cfg.get('ssh_port',22), username=cfg['ssh_user'], key_filename=cfg['ssh_private_key_path'])
    _stdin, stdout, stderr=client.exec_command(cmd)
    returncode=stdout.channel.recv_exit_status()
    out=stdout.read().decode(); err=stderr.read().decode()
    client.close()
    class Result: pass
    res=Result(); res.returncode=returncode; res.stdout=out; res.stderr=err
    if out.strip():
        print(out, end='' if out.endswith('\n') else '\n')
    if err.strip():
        print(err, end='' if err.endswith('\n') else '\n', file=sys.stderr)
    if check and returncode!=0:
        die(f"Remote command failed ({cmd}): {err or out}")
    return res

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',required=True); ap.add_argument('--state',required=True); args=ap.parse_args()
    cfg=load_yaml(Path(args.config))['proxmox']; sj=Path(args.state)/'started_vmids.json'
    if not sj.exists(): die(f"State file not found: {sj}")
    vmids=json.loads(sj.read_text()).get('vmids',[])
    print('=== qm list ==='); ssh_run(cfg,'qm list')
    for vmid in vmids:
        vmid=int(vmid); print(f"=== qm status {vmid} ==="); ssh_run(cfg,f"qm status {vmid}")
if __name__=='__main__': main()
