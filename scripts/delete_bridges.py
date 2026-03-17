
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

def ssh_run(cfg: dict, cmd: str, check: bool=True):
    full=ssh_base(cfg)+[cmd]; print('+',' '.join(full))
    return subprocess.run(full,text=True,capture_output=True,check=check)

def has_ports(cfg: dict, br: str)->bool:
    cp=ssh_run(cfg,f"ip -o link show master {br} | wc -l",check=False)
    try: return int(cp.stdout.strip())>0
    except Exception: return False

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--config',required=True); ap.add_argument('--state-dir',required=True)
    ap.add_argument('--force',action='store_true'); ap.add_argument('--no-apply',action='store_true'); args=ap.parse_args()
    cfg=load_yaml(Path(args.config))['proxmox']; state=Path(args.state_dir); sj=state/'created_bridges.json'
    if not sj.exists(): die(f"State file not found: {sj}")
    payload=json.loads(sj.read_text())
    bridges=payload.get('newly_created')
    if bridges is None:
        bridges=payload.get('bridges',[])
    if not bridges:
        print('No created bridges recorded; nothing to delete.')
        return
    print(f"[INFO] Bridges selected for deletion: {len(bridges)}")
    for br in bridges:
        print(f"\n=== Delete bridge {br} ===")
        if (not args.force) and has_ports(cfg, br): print(f"[SKIP] {br} has member ports; use --force to override"); continue
        cmd_rm=(
            "python3 - <<'PY'\n"
            "p='/etc/network/interfaces'\n"
            "txt=open(p).read().splitlines(True)\n"
            f"br='{br}'\n"
            "out=[]\n"
            "i=0\n"
            "while i < len(txt):\n"
            "  line=txt[i]\n"
            "  if line.strip()==f'auto {br}':\n"
            "    i+=1\n"
            "    while i < len(txt) and txt[i].strip()!='': i+=1\n"
            "    if i < len(txt) and txt[i].strip()=='' : i+=1\n"
            "    continue\n"
            "  out.append(line); i+=1\n"
            "open(p,'w').write(''.join(out))\n"
            "PY"
        )
        ssh_run(cfg, cmd_rm, check=False)
        ssh_run(cfg, f"ip link show {br} >/dev/null 2>&1 && ip link delete {br} type bridge || true", check=False)
    if not args.no_apply: ssh_run(cfg,'ifreload -a',check=False)
    ssh_run(cfg,"ip -o link show | grep vmc_ || true",check=False)
if __name__=='__main__': main()
