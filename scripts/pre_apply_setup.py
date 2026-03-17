#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, json, os, subprocess, sys
from pathlib import Path
import paramiko
try:
    import yaml
except Exception:
    yaml=None

REPO_ROOT=Path(__file__).resolve().parents[1]
DEFAULT_VMM_FILE=REPO_ROOT/'output'/'input.vmm'

def die(msg: str, code: int=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)

def load_yaml(p: Path)->dict:
    if yaml is None:
        die('pyyaml required: pip3 install pyyaml')
    return yaml.safe_load(p.read_text()) or {}

def ssh_run(cfg: dict, cmd: str, check: bool=True):
    client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(cfg['host'], port=cfg.get('ssh_port',22), username=cfg['ssh_user'], key_filename=cfg['ssh_private_key_path'])
    _stdin, stdout, stderr=client.exec_command(cmd)
    returncode=stdout.channel.recv_exit_status(); out=stdout.read().decode(); err=stderr.read().decode(); client.close()
    class Result: pass
    res=Result(); res.returncode=returncode; res.stdout=out; res.stderr=err
    if check and returncode!=0:
        die(f"Remote command failed ({cmd}): {err or out}")
    return res

def remote_ls(cfg: dict, path: str)->list[str]:
    cp=ssh_run(cfg,f"ls -1 {path}",check=False)
    if cp.returncode!=0:
        return []
    files=[ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
    files.sort(); return files

def remote_exists(cfg: dict, path: str)->bool:
    return ssh_run(cfg,f"test -f {path}",check=False).returncode==0

def read_choice(prompt: str)->str:
    if sys.stdin.isatty():
        try:
            return input(prompt)
        except EOFError:
            pass
    tty_path='CONIN$' if os.name=='nt' else '/dev/tty'
    try:
        print(prompt, end='', flush=True)
        with open(tty_path,'r',encoding='utf-8',errors='ignore') as tty:
            line=tty.readline()
        if line=='':
            raise EOFError()
        return line.rstrip('\r\n')
    except Exception:
        raise EOFError()

def prompt_pick(vm_name: str, vmid: int, image_dir: str, files: list[str])->str|None:
    print(f"\n[SELECT] Provide disk image for {vm_name} (VMID {vmid})")
    if files:
        print(f"  Available in {image_dir}:")
        for idx,fname in enumerate(files,1):
            print(f"    {idx}) {fname}")
    else:
        print(f"  No files auto-detected under {image_dir}. You may still type a path manually.")
    print("  s) skip")
    while True:
        try:
            choice=read_choice('Enter number, relative filename, absolute path, or s to skip: ').strip()
        except EOFError:
            die(
                f"Non-interactive stdin detected while prompting for {vm_name} (VMID {vmid}). "
                "Run this command in an interactive terminal, or pre-populate --selection-file "
                "(../state/selected_images.json) with required image paths.",
                code=2,
            )
        if not choice:
            continue
        low=choice.lower()
        if low=='s':
            return None
        if choice.isdigit() and files:
            idx=int(choice)
            if 1<=idx<=len(files):
                return f"{image_dir}/{files[idx-1]}"
        if choice.startswith('/'):
            return choice
        return f"{image_dir}/{choice}"

def ensure_state_dir(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)

SUFFIXES={'vmx-re':('.qcow2','.vmdk','.raw','.img'),'vmx-mpc':('.qcow2','.vmdk','.raw','.img'),'vqfx-re':('.qcow2','.vmdk','.raw','.img'),'vqfx-pfe':('.qcow2','.vmdk','.raw','.img'),'vsrx-re':('.qcow2','.vmdk','.raw','.img'),'vptx-cspp':('.qcow2','.vmdk','.raw','.img'),'linux':('.qcow2','.vmdk','.raw','.img'),'modem':('.qcow2','.vmdk','.raw','.img')}

PRIORITY={'vmx-re':1,'vmx-mpc':2,'vqfx-re':3,'vqfx-pfe':4,'vsrx-re':5,'vptx-re':6,'vptx-cspp':7,'linux':8,'modem':9}

def needs_disk(vm: dict)->bool:
    return vm.get('vmtype') in SUFFIXES

def run_create_bridges(config: str, vmm_file: str|None, state: str):
    script=Path(__file__).with_name('create_bridges.py')
    vmm_arg=str(Path(vmm_file).expanduser()) if vmm_file else str(DEFAULT_VMM_FILE)
    cmd=[sys.executable, str(script), '--config', config, '--vmm-file', vmm_arg, '--state-dir', state]
    print('[STEP] Ensuring vmc_* bridges exist ...')
    result=subprocess.run(cmd, check=True, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print('[STDERR]', result.stderr, file=sys.stderr)
    # Verify state file was created
    state_file=Path(state)/'created_bridges.json'
    if not state_file.exists():
        die(f'[ERROR] Expected state file not created: {state_file}')
    try:
        payload=json.loads(state_file.read_text())
        print(f"[OK] Bridge state recorded: {len(payload.get('bridges',[]))} total, {len(payload.get('newly_created',[]))} newly created")
    except Exception as exc:
        die(f'[ERROR] Could not parse bridge state file {state_file}: {exc}')

def gather_selections(cfg: dict, plan: dict, image_dir: str, selections: dict[str,str])->dict[str,str]:
    updated=dict(selections)
    vms=plan.get('vms', [])
    vms_sorted=sorted(vms, key=lambda v: (PRIORITY.get(v.get('vmtype',''),99), int(v['vmid'])))
    for vm in vms_sorted:
        vmid=str(vm['vmid']); vt=vm.get('vmtype'); name=vm.get('name')
        if vt not in SUFFIXES:
            continue
        expected=vm.get('expected_image')
        if expected:
            candidate=expected if str(expected).startswith('/') else f"{image_dir}/{expected}"
            if remote_exists(cfg,candidate):
                print(f"[OK] {name} uses expected image {candidate}")
                updated.pop(vmid, None)
                continue
        existing=updated.get(vmid)
        if existing and remote_exists(cfg, existing):
            print(f"[OK] {name} retains previously selected {existing}")
            continue
        files=[f for f in remote_ls(cfg,image_dir) if f.lower().endswith(SUFFIXES[vt])]
        while True:
            selected=prompt_pick(name, int(vmid), image_dir, files)
            if not selected:
                print(f"[WARN] No image selected for {name}; this VM will be skipped unless resolved later.")
                updated.pop(vmid, None)
                break
            if remote_exists(cfg, selected):
                updated[vmid]=selected
                print(f"[SET] {name} -> {selected}")
                break
            print(f"[WARN] {selected} does not exist on remote host. Pick again.")
    return updated

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--config',default='../config.yaml')
    ap.add_argument('--plan',default='../output/plan/attach_plan.json')
    ap.add_argument('--type-registry',default='../type_registry.yaml')
    ap.add_argument('--overrides',default='../overrides.yaml')
    ap.add_argument('--vmm-file',default=str(DEFAULT_VMM_FILE))
    ap.add_argument('--state-dir',default='../state')
    ap.add_argument('--selection-file',default='../state/selected_images.json')
    ap.add_argument('--skip-bridges',action='store_true')
    args=ap.parse_args()

    cfg=load_yaml(Path(args.config))['proxmox']
    reg=load_yaml(Path(args.type_registry))
    plan=json.loads(Path(args.plan).read_text())

    image_dir=cfg.get('image_dir', reg.get('behavior',{}).get('image_dir','/root/import'))
    ensure_state_dir(Path(args.state_dir))
    if not args.skip_bridges:
        run_create_bridges(args.config, args.vmm_file, args.state_dir)

    sel_path=Path(args.selection_file)
    existing={}
    if sel_path.is_file():
        try:
            payload=json.loads(sel_path.read_text())
            existing=payload.get('selections') or {}
        except Exception as exc:
            print(f"[WARN] Could not parse {sel_path}: {exc}")
    updated=gather_selections(cfg, plan, image_dir, existing)
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    sel_path.write_text(json.dumps({'selections': updated}, indent=2))
    print(f"[DONE] Recorded selections for {len(updated)} VM(s) in {sel_path}")

if __name__=='__main__':
    main()
