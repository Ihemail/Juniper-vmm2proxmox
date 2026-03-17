#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
import shlex
from pathlib import Path
try:
    import paramiko
except Exception:
    paramiko=None
try:
    import yaml
except Exception:
    yaml=None

def die(msg: str, code: int=1): print(f"ERROR: {msg}", file=sys.stderr); raise SystemExit(code)

def load_yaml(p: Path)->dict:
    if yaml is None: die('pyyaml required on runner: pip3 install pyyaml')
    return yaml.safe_load(p.read_text()) or {}

def normalize_path_arg(raw: str|None)->Path|None:
    if not raw:
        return None
    raw=raw.strip()
    if raw and raw[0]==raw[-1] and raw[0] in '"\'':
        raw=raw[1:-1]
    return Path(raw).expanduser()

#def ssh_base(cfg: dict)->list[str]:
#    return ['ssh','-i',cfg['ssh_private_key_path'],'-p',str(cfg.get('ssh_port',22)),'-o','StrictHostKeyChecking=no',f"{cfg['ssh_user']}@{cfg['host']}"]

def ssh_run(cfg: dict, cmd: str, check: bool=True):
    if paramiko is None:
        die('paramiko required on runner: pip3 install paramiko')
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(cfg['host'], port=cfg.get('ssh_port', 22), username=cfg['ssh_user'], key_filename=cfg['ssh_private_key_path'])
    stdin, stdout, stderr = client.exec_command(cmd)
    returncode = stdout.channel.recv_exit_status()
    client.close()
    
    class Result:
        pass
    result = Result()
    result.returncode = returncode
    result.stdout = stdout.read().decode()
    result.stderr = stderr.read().decode()
    return result

def remote_ls(cfg: dict, path: str)->list[str]:
    cp=ssh_run(cfg,f"ls -1 {path}",check=False)
    if cp.returncode!=0: return []
    files=[ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
    files.sort(); return files

def selection_for_vm(cfg: dict, selections: dict[str,str], vmid: int, image_dir: str)->str|None:
    if not selections: return None
    sel=selections.get(str(vmid))
    if not sel: return None
    candidate=sel if sel.startswith('/') else f"{image_dir}/{sel}"
    if ssh_run(cfg,f"test -f {candidate}",check=False).returncode==0: return candidate
    print(f"WARN: Selected image {candidate} for VM {vmid} not found on remote host; ignoring entry.")
    return None

def prompt_pick(prompt: str, files: list[str]) -> str | None:
    if not files:
        print(prompt); print('  (no files found)'); return None
    print(prompt)
    for i,f in enumerate(files,1): print(f"  {i}) {f}")
    print('  s) skip')
    if not sys.stdin.isatty():
        print('  (non-interactive session detected, skipping selection)')
        return None
    while True:
        try:
            s=input('Select: ')
        except EOFError:
            print('\n  (input unavailable, skipping selection)')
            return None
        s=s.strip().lower()
        if s=='s': return None
        if s.isdigit() and 1<=int(s)<=len(files): return files[int(s)-1]

def qm_config(cfg: dict, vmid: int)->str:
    cp=ssh_run(cfg,f"qm config {vmid}",check=False)
    return cp.stdout if cp.returncode==0 else ''

def find_unused_volume(cfg_text: str, storage_id: str)->str|None:
    unused=[]
    for line in cfg_text.splitlines():
        if line.startswith('unused') and storage_id in line:
            parts=line.split(':',2)
            if len(parts)>=3: unused.append(parts[2].strip())
    return unused[-1] if unused else None

def list_folder_content(cfg: dict, folder_location: str):
    print(f"[INFO] Listing contents of folder '{folder_location}'")
    resp=ssh_run(cfg,f"ls -ltr {folder_location}",check=False)
    if resp.stdout:
        print(resp.stdout.strip())
    if resp.stderr:
        print(resp.stderr.strip())
    print(f"[INFO] pvesm exit code: {resp.returncode}")

def importdisk_and_attach(cfg: dict, vmid: int, src_path: str, storage_id: str, slot: str, fmt: str, retries: int=5, delay: int=3):
    print(f"[INFO] qm importdisk {vmid} {src_path} {storage_id} --format {fmt}")
    print(f"[INFO] qm importdisk {vmid} {src_path} {storage_id} --format {fmt}")
    resp=ssh_run(cfg,f"qm importdisk {vmid} {src_path} {storage_id} --format {fmt}",check=False)
    if resp.stdout:
        print('[qm importdisk stdout]\n'+resp.stdout.strip())
    if resp.stderr:
        print('[qm importdisk stderr]\n'+resp.stderr.strip())
    print(f"[INFO] qm importdisk exit code: {resp.returncode}")
    vol=None
    for attempt in range(retries):
        ssh_run(cfg,f"qm disk rescan")
        vol=find_unused_volume(qm_config(cfg,vmid), storage_id)
        if vol:
            break
        time.sleep(delay)
    if not vol:
        print(f"[INFO] qm config {vmid} output:")
        print(qm_config(cfg,vmid) or '(empty)')
        #list_folder_content(cfg, f"/etc/pve/qemu-server/")
        die(f"Could not find unused volume after importdisk for VMID {vmid}. Check qm importdisk output and storage usage.")
    ssh_run(cfg,f"qm set {vmid} --{slot} {storage_id}:{vol}")

def ensure_iso_on_local(cfg: dict, image_dir: str, iso_storage_path: str, iso_name: str):
    dst=f"{iso_storage_path}/{iso_name}"; src=f"{image_dir}/{iso_name}"
    ssh_run(cfg, f"test -f {dst} || cp {src} {dst}", check=False)

def attach_iso_cdrom(cfg: dict, vmid: int, iso_storage_id: str, iso_filename: str):
    ssh_run(cfg,f"qm set {vmid} --ide2 {iso_storage_id}:iso/{iso_filename},media=cdrom")

def resolve_iso_from_storage(cfg: dict, iso_storage_path: str, interactive: bool, expected_iso: str|None)->str|None:
    if expected_iso:
        iso_name=os.path.basename(expected_iso)
        if ssh_run(cfg,f"test -f {iso_storage_path}/{iso_name}",check=False).returncode==0:
            return iso_name
    if interactive:
        files=[f for f in remote_ls(cfg,iso_storage_path) if f.lower().endswith('.iso')]
        return prompt_pick(f"Pick CDROM ISO from {cfg['host']}:{iso_storage_path}", files)
    return None

def resolve_remote(cfg: dict, image_dir: str, interactive: bool, overrides: dict, default_key: str, expected: str|None, suffix: tuple[str,...]):
    if expected:
        if expected.startswith('/'):
            if ssh_run(cfg,f"test -f {expected}",check=False).returncode==0: return expected
        else:
            if ssh_run(cfg,f"test -f {image_dir}/{expected}",check=False).returncode==0: return f"{image_dir}/{expected}"
    if interactive:
        files=[f for f in remote_ls(cfg,image_dir) if f.lower().endswith(suffix)]
        chosen=prompt_pick(f"Missing expected file. Pick from {cfg['host']}:{image_dir}", files)
        return f"{image_dir}/{chosen}" if chosen else None
    d=(overrides.get('default_images') or {}).get(default_key)
    if d and ssh_run(cfg,f"test -f {image_dir}/{d}",check=False).returncode==0: return f"{image_dir}/{d}"
    return None

def qm_status(cfg: dict, vmid: int)->str:
    cp=ssh_run(cfg,f"qm status {vmid}",check=False)
    return cp.stdout.strip() if cp.returncode==0 else ''

def wait_running(cfg: dict, vmid: int, timeout: int, poll: int=5)->bool:
    start=time.time()
    while True:
        st=qm_status(cfg,vmid)
        if 'running' in st: print(f"[OK] VM {vmid} is running."); return True
        el=int(time.time()-start)
        if el>=timeout: print(f"[TIMEOUT] VM {vmid} not running within {timeout}s. Last: {st}"); return False
        print(f"[WAIT] VM {vmid} not running yet ({st or 'no status'}). Elapsed {el}s ..."); time.sleep(poll)

def set_boot_order(cfg: dict, vmid: int, boot_order: str):
    ssh_run(cfg, f"qm set {vmid} --boot {shlex.quote(boot_order)}", check=False)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--config',required=True)
    ap.add_argument('--plan',required=True)
    ap.add_argument('--type-registry',required=True)
    ap.add_argument('--overrides',required=True)
    ap.add_argument('--state-dir',required=True)
    ap.add_argument('--start-interval',type=int,default=30)
    ap.add_argument('--start-timeout',type=int,default=180)
    ap.add_argument('--image-selection')
    args=ap.parse_args()

    cfg=load_yaml(Path(args.config))['proxmox']
    reg=load_yaml(Path(args.type-registry)) if False else load_yaml(Path(args.type_registry))
    ov=load_yaml(Path(args.overrides))
    plan=json.loads(Path(args.plan).read_text())

    selections={}
    if args.image_selection:
        sel_path=normalize_path_arg(args.image_selection)
        if sel_path:
            if sel_path.is_file():
                try:
                    payload=json.loads(sel_path.read_text())
                    selections=payload.get('selections') or {}
                except Exception as exc:
                    print(f"WARN: Could not parse image selection file {sel_path}: {exc}")
            else:
                print(f"[INFO] Image selection file {sel_path} not found; proceeding without presets.")

    beh=reg.get('behavior') or {}
    interactive=bool(beh.get('interactive',False))
    unknown_action=beh.get('unknown_action','skip')

    image_dir=cfg.get('image_dir', beh.get('image_dir','/root/import'))
    storage_id=beh.get('storage_id', plan.get('storage_id'))
    iso_storage_id=cfg.get('iso_storage_id', beh.get('iso_storage_id','local'))
    iso_storage_path=cfg.get('iso_storage_path', beh.get('iso_storage_path','/var/lib/vz/template/iso'))

    vms=plan.get('vms', [])
    priority={'vmx-re':1,'vmx-mpc':2,'vqfx-re':3,'vqfx-pfe':4,'vsrx-re':5,'vptx-re':6,'vptx-cspp':7,'linux':8,'modem':9}
    vms_sorted=sorted(vms, key=lambda v: (priority.get(v.get('vmtype',''),99), int(v['vmid'])))
    vmids=[int(v['vmid']) for v in vms_sorted]; vmap={int(v['vmid']): v for v in vms}

    sdir=Path(args.state_dir); sdir.mkdir(parents=True, exist_ok=True)

    for vmid in vmids:
        v=vmap[vmid]; vt=v.get('vmtype'); print(f"\n=== Attach: {vmid} {v.get('name')} ({vt}) ===")
        preset=selection_for_vm(cfg,selections,vmid,image_dir)
        if vt=='vptx-re':
            extra=v.get('extra_ide0') or reg.get('types',{}).get('vptx',{}).get('re',{}).get('extra_disk_ide0')
            if extra:
                ep=extra if extra.startswith('/') else f"{image_dir}/{extra}"
                if ssh_run(cfg,f"test -f {ep}",check=False).returncode==0:
                    fmt='vmdk' if ep.lower().endswith('.vmdk') else 'qcow2' if ep.lower().endswith('.qcow2') else 'raw'
                    importdisk_and_attach(cfg,vmid,ep,storage_id,'ide0',fmt)
                else:
                    msg=f"Missing vPTX RE ide0 disk {ep}"; print('WARN:', msg)
                    if unknown_action=='fail': die(msg)
                    continue
            else:
                msg=f"No vPTX RE ide0 disk source configured for {v.get('name')}"; print('WARN:', msg)
                if unknown_action=='fail': die(msg)
                continue

            iso=v.get('iso_name')
            iso_fn=resolve_iso_from_storage(cfg,iso_storage_path,interactive,iso)
            if iso_fn:
                attach_iso_cdrom(cfg,vmid,iso_storage_id,iso_fn)
            else:
                msg=f"Missing ISO in {iso_storage_path} for vPTX RE {v.get('name')}"; print('WARN:', msg)
                if unknown_action=='fail': die(msg)
            set_boot_order(cfg, vmid, v.get('boot_after_attach') or 'order=ide0;ide2'); continue
        if vt=='vptx-cspp':
            fn=preset or resolve_remote(cfg,image_dir,interactive,ov,'vptx_cspp',v.get('expected_image'),('.qcow2','.vmdk','.raw'))
            if not fn:
                msg=f"Missing CSPP ide0 image for {v.get('name')}"; print('WARN:',msg)
                if unknown_action=='fail': die(msg)
                continue
            fmt='qcow2' if fn.lower().endswith('.qcow2') else 'vmdk' if fn.lower().endswith('.vmdk') else 'raw'
            importdisk_and_attach(cfg,vmid,fn,storage_id,'ide0',fmt); set_boot_order(cfg, vmid, v.get('boot_after_attach') or 'order=ide0'); continue
        if vt=='vmx-re':
            fn=preset or resolve_remote(cfg,image_dir,interactive,ov,'vmx_re',v.get('expected_image'),('.qcow2','.vmdk','.raw'))
            if not fn:
                msg=f"Missing vMX RE ide0 image for {v.get('name')}"; print('WARN:',msg)
                if unknown_action=='fail': die(msg)
                continue
            fmt='vmdk' if fn.lower().endswith('.vmdk') else 'qcow2' if fn.lower().endswith('.qcow2') else 'raw'
            importdisk_and_attach(cfg,vmid,fn,storage_id,'ide0',fmt)
            extra=v.get('extra_ide1') or reg.get('types',{}).get('vmx',{}).get('re',{}).get('extra_disk_ide1')
            if extra:
                ep=extra if extra.startswith('/') else f"{image_dir}/{extra}"
                if ssh_run(cfg,f"test -f {ep}",check=False).returncode==0:
                    fmt2='qcow2' if ep.lower().endswith('.qcow2') else 'vmdk' if ep.lower().endswith('.vmdk') else 'raw'
                    importdisk_and_attach(cfg,vmid,ep,storage_id,'ide1',fmt2)
                else:
                    msg=f"Missing vMX RE ide1 disk {ep}"; print('WARN:', msg); 
                    if unknown_action=='fail': die(msg)
            set_boot_order(cfg, vmid, v.get('boot_after_attach') or 'order=ide0'); continue
        if vt=='vmx-mpc':
            fn=preset or resolve_remote(cfg,image_dir,interactive,ov,'vmx_mpc',v.get('expected_image'),('.qcow2','.vmdk','.raw'))
            if not fn:
                msg=f"Missing vMX MPC ide0 image for {v.get('name')}"; print('WARN:',msg)
                if unknown_action=='fail': die(msg)
                continue
            fmt='vmdk' if fn.lower().endswith('.vmdk') else 'qcow2' if fn.lower().endswith('.qcow2') else 'raw'
            importdisk_and_attach(cfg,vmid,fn,storage_id,'ide0',fmt); set_boot_order(cfg, vmid, v.get('boot_after_attach') or 'order=ide0'); continue
        if vt=='vqfx-re':
            fn=preset or resolve_remote(cfg,image_dir,interactive,ov,'vqfx_re',v.get('expected_image'),('.qcow2','.vmdk','.raw','.img'))
            if not fn:
                msg=f"Missing vQFX RE ide0 image for {v.get('name')}"; print('WARN:',msg)
                if unknown_action=='fail': die(msg)
                continue
            fmt='vmdk' if fn.lower().endswith('.vmdk') else 'qcow2' if fn.lower().endswith('.qcow2') else 'raw'
            importdisk_and_attach(cfg,vmid,fn,storage_id,'ide0',fmt); set_boot_order(cfg, vmid, v.get('boot_after_attach') or 'order=ide0'); continue
        if vt=='vqfx-pfe':
            fn=preset or resolve_remote(cfg,image_dir,interactive,ov,'vqfx_pfe',v.get('expected_image'),('.qcow2','.vmdk','.raw','.img'))
            if not fn:
                msg=f"Missing vQFX PFE ide0 image for {v.get('name')}"; print('WARN:',msg)
                if unknown_action=='fail': die(msg)
                continue
            fmt='vmdk' if fn.lower().endswith('.vmdk') else 'qcow2' if fn.lower().endswith('.qcow2') else 'raw'
            importdisk_and_attach(cfg,vmid,fn,storage_id,'ide0',fmt); set_boot_order(cfg, vmid, v.get('boot_after_attach') or 'order=ide0'); continue
        if vt=='vsrx-re':
            fn=preset or resolve_remote(cfg,image_dir,interactive,ov,'vsrx',v.get('expected_image'),('.qcow2','.vmdk','.raw','.img'))
            if not fn:
                msg=f"Missing vSRX ide0 image for {v.get('name')}"; print('WARN:',msg)
                if unknown_action=='fail': die(msg)
                continue
            fmt='vmdk' if fn.lower().endswith('.vmdk') else 'qcow2' if fn.lower().endswith('.qcow2') else 'raw'
            importdisk_and_attach(cfg,vmid,fn,storage_id,'ide0',fmt); set_boot_order(cfg, vmid, v.get('boot_after_attach') or 'order=ide0'); continue
        if vt in ('linux','modem'):
            key='linux' if vt=='linux' else 'modem'; fn=preset or resolve_remote(cfg,image_dir,interactive,ov,key,v.get('expected_image'),('.qcow2','.vmdk','.raw'))
            if not fn:
                msg=f"Missing ide0 image for {v.get('name')} ({vt})"; print('WARN:',msg)
                if unknown_action=='fail': die(msg)
                continue
            fmt='vmdk' if fn.lower().endswith('.vmdk') else 'qcow2' if fn.lower().endswith('.qcow2') else 'raw'
            importdisk_and_attach(cfg,vmid,fn,storage_id,'ide0',fmt); set_boot_order(cfg, vmid, v.get('boot_after_attach') or 'order=ide0'); continue
        print('WARN: unhandled vmtype', vt)

    started=[]; print("\n=== Start (dependency order, wait running) ===")
    for vmid in vmids:
        print(f"[START] VM {vmid}"); ssh_run(cfg,f"qm start {vmid}",check=False)
        ok=wait_running(cfg,vmid,args.start_timeout)
        if ok: started.append(vmid); print(f"[LOG] VM {vmid} running; started: {started}")
        print(f"[SLEEP] {args.start_interval}s"); time.sleep(args.start_interval)
        if (not ok) and unknown_action=='fail': die(f"VM {vmid} failed to reach running state")
    print("\n=== qm list ==="); cp=ssh_run(cfg,'qm list',check=False); print(cp.stdout)
    sdir=Path(args.state_dir); (sdir/'started_vmids.json').write_text(json.dumps({'vmids':started},indent=2)); (sdir/'started_vmids.txt').write_text("\n".join(map(str,started))+"\n"); (sdir/'qm_list.txt').write_text(cp.stdout)
if __name__=='__main__': main()
