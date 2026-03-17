
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, json, sys, time
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

def ssh_run(cfg: dict, cmd: str, check: bool=True):
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

def qm_status(cfg: dict, vmid: int)->str:
    cp=ssh_run(cfg,f"qm status {vmid}",check=False)
    return cp.stdout.strip() if cp.returncode==0 else ''

def wait_stopped(cfg: dict, vmid: int, timeout: int, poll: int=5)->bool:
    start=time.time()
    while True:
        st=qm_status(cfg,vmid)
        if 'stopped' in st: print(f"[OK] VM {vmid} stopped"); return True
        el=int(time.time()-start)
        if el>=timeout: print(f"[TIMEOUT] VM {vmid} not stopped within {timeout}s. Last: {st}"); return False
        print(f"[WAIT] VM {vmid} not stopped yet ({st or 'no status'}). Elapsed {el}s ..."); time.sleep(poll)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',required=True); ap.add_argument('--state',required=True); ap.add_argument('--timeout',type=int,default=600); ap.add_argument('--interval',type=int,default=10); ap.add_argument('--force',action='store_true'); args=ap.parse_args()
    cfg=load_yaml(Path(args.config))['proxmox']; sj=Path(args.state)/'started_vmids.json'
    if not sj.exists(): print('No started_vmids.json; nothing to shutdown.'); return
    vmids=json.loads(sj.read_text()).get('vmids',[])
    if not vmids: print('No VMIDs recorded; nothing to shutdown.'); return
    for vmid in reversed(vmids):
        vmid=int(vmid); print(f"[SHUTDOWN] VM {vmid}"); ssh_run(cfg,f"qm shutdown {vmid}",check=False)
        ok=wait_stopped(cfg,vmid,args.timeout)
        if (not ok) and args.force:
            print(f"[FORCE] qm stop {vmid}"); ssh_run(cfg,f"qm stop {vmid}",check=False); wait_stopped(cfg,vmid,60)
        time.sleep(args.interval)
    print("\n=== qm list ==="); ssh_run(cfg,'qm list',check=False)
if __name__=='__main__': main()
