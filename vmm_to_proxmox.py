#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, dataclasses, hashlib, json, os, re, sys
from pathlib import Path
import shutil
try:
    import yaml
except Exception:
    yaml=None

REPO_ROOT=Path(__file__).resolve().parent
CANONICAL_VMM_PATH=REPO_ROOT/'output'/'input.vmm'

def die(msg: str, code: int=1): print(f"ERROR: {msg}", file=sys.stderr); raise SystemExit(code)

def load_yaml(p: Path)->dict:
    if yaml is None: die('pyyaml is required: pip3 install pyyaml')
    return yaml.safe_load(p.read_text()) or {}

def strip_comments(text: str)->str: return re.sub(r"//.*$", "", text, flags=re.MULTILINE)

def normalize_bridge(name: str)->str: return re.sub(r"[^a-z0-9]", "", name.lower())

def proxmox_vm_name(name: str)->str: return name.replace('_','-')

def vmbrc(name: str)->str: return f"vmc_{normalize_bridge(name)[:6]}"

def build_bridge_map(names: set[str])->dict[str,str]:
    out={}
    used=set()
    for name in sorted(names):
        base=vmbrc(name)
        br=base
        idx=2
        while br in used:
            br=f"{base}_{idx}"
            idx+=1
        used.add(br)
        out[name]=br
    return out

def mac_for(prefix: str, vm_name: str, vmid: int, net_index: int, used: set[str])->str:
    p = prefix.upper().split(':')
    if len(p) != 3: die(f"mac_prefix must be 3 octets like BC:24:22 (got {prefix})")
    base = [int(x, 16) for x in p]
    salt=0
    while True:
        h=hashlib.sha1(f"{vm_name}:{vmid}:{net_index}:{salt}".encode()).digest()
        x,y,z=h[0],h[1],h[2]
        if (x,y,z)==(0,0,0): z=1
        mac=f"{base[0]:02X}:{base[1]:02X}:{base[2]:02X}:{x:02X}:{y:02X}:{z:02X}"
        if mac not in used:
            used.add(mac); return mac
        salt+=1

@dataclasses.dataclass
class Nic:
    net:int; model:str; bridge:str; vlan:int|None=None

@dataclasses.dataclass
class VM:
    vmid:int; name:str; vmtype:str; machine:str; cores:int; memory:int; nics:list[Nic]
    expected_image:str|None=None; extra_ide1:str|None=None; extra_ide0:str|None=None
    iso_name:str|None=None; boot_after_attach:str|None=None
    conf_args:str|None=None; scsihw:str|None=None

VM_BLOCK_RE=re.compile(r"vm\s+\"(?P<name>[^\"]+)\"\s*\{(?P<body>.*?)^\s*\};", re.DOTALL|re.MULTILINE)

def parse_basedisk_defines(text: str)->dict[str,str]:
    m={}
    for dm in re.finditer(r"#define\s+(\w+)\s+basedisk\s+\"([^\"]+)\"\s*;", text): m[dm.group(1)] = os.path.basename(dm.group(2))
    for dm in re.finditer(r"#define\s+(EVOVPTX_DISK1|EVOVPTX_FPC_CSPP_IMG|VMX_DISK|VQFX10_DISK|COSIM_DISK|VSRX_DISK)\s+\"([^\"]+)\"", text): m[dm.group(1)] = os.path.basename(dm.group(2))
    return m

def parse_regular_vms(text: str):
    vms=[]
    for m in VM_BLOCK_RE.finditer(text):
        name=m.group('name').strip(); body=m.group('body')
        vm={'name':name,'ncpus':None,'memory':None,'interfaces':[],'tokens':[],'token':None}

        mm_cpu=re.search(r"\bncpus\s+(\d+)\s*;", body)
        if mm_cpu:
            vm['ncpus']=int(mm_cpu.group(1))
        mm_mem=re.search(r"\bmemory\s+(\d+)\s*;", body)
        if mm_mem:
            vm['memory']=int(mm_mem.group(1))

        for im in re.finditer(r"interface\s+\"(?:em)?(?P<idx>\d+)\"\s*\{(?P<ibody>.*?)\}\s*;", body, flags=re.DOTALL):
            idx=int(im.group('idx'))
            ibody=im.group('ibody')
            bm=re.search(r"bridge\s+\"([^\"]+)\"", ibody)
            bridge=bm.group(1) if bm else None
            vm['interfaces'].append({'ifindex': idx, 'bridge': bridge, 'external': bool(re.search(r"\bEXTERNAL\s*;", ibody))})

        for tm in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*;?\s*$", body, flags=re.MULTILINE):
            token=tm.group(1)
            if token in ('hostname','setvar','memory','ncpus','interface'):
                continue
            vm['tokens'].append(token)

        vms.append((m.start(), vm))
    return vms

def token_candidates(token: str|None)->list[str]:
    if not token:
        return []
    out=[token]
    if token.startswith('AD_') and len(token)>3:
        out.append(token[3:])
    return out

def resolve_regular_token(vm: dict, token_map: dict, disk_map: dict)->str|None:
    tokens=list(vm.get('tokens') or [])
    for t in tokens:
        for cand in token_candidates(t):
            if cand in token_map:
                return cand
    for t in tokens:
        for cand in token_candidates(t):
            if cand.endswith('_base') or cand.endswith('_DISK') or cand in disk_map:
                return cand
    return None

def nearest_define(text: str, upto: int, name: str)->str|None:
    last=None; pat=re.compile(rf"#define\s+{re.escape(name)}\s+([A-Za-z0-9_\-]+)")
    for m in pat.finditer(text,0,upto): last=m.group(1)
    return last

def parse_vmx_chassis(text: str):
    res=[]
    for m in re.finditer(r"VMX_CHASSIS_START\(\)\s*(?P<body>.*?)\s*VMX_CHASSIS_END", text, flags=re.DOTALL):
        start=m.start(); body=m.group('body'); chname=nearest_define(text,start,'VMX_CHASSIS_NAME') or f"vmx_chassis_{len(res)+1}"
        mpcs=[]
        for mm in re.finditer(r"VMX_MPC_START\((?P<label>[^,]+),(?P<idx>\d+)\)\s*(?P<mbody>.*?)\s*VMX_MPC_END", body, flags=re.DOTALL):
            idx=int(mm.group('idx')); mbody=mm.group('mbody'); ge=[]
            for cm in re.finditer(r"VMX_CONNECT\(\s*GE\(\s*0\s*,\s*0\s*,\s*(\d+)\s*\)\s*,\s*([A-Za-z0-9_]+)\s*\)", mbody): ge.append(cm.group(2))
            mpcs.append({'index':idx,'ge_bridges':ge})
        res.append((start,{'name':chname,'mpcs':sorted(mpcs,key=lambda x:x['index'])}))
    return res

def parse_vptx_chassis(text: str):
    res=[]
    for m in re.finditer(r"EVOVPTX_CHASSIS_START_\s*\(\s*PTX_CHAS_NAME\s*\)\s*(?P<body>.*?)\s*EVOVPTX_CHASSIS_END_", text, flags=re.DOTALL):
        start=m.start(); body=m.group('body'); re_name=nearest_define(text,start,'PTX_CHAS_NAME') or f"vPTX_chassis_{len(res)+1}"
        base=re_name[:-3] if re_name.endswith('-re') else re_name; cspp=[]
        for mm in re.finditer(r"(?:EVOvArdbeg_CSPP_START|EVOvBrackla_CSPP_START)\(.*?\)\s*(?P<cbody>.*?)\s*(?:EVOvArdbeg_CSPP_END|EVOvBrackla_CSPP_END)", body, flags=re.DOTALL):
            cbody=mm.group('cbody'); ifs=[]
            for cm in re.finditer(r"EVOVPTX_CONNECT\(\s*IF_ET\s*\(\s*[01]\s*,\s*0\s*,\s*(\d+)\s*\)\s*,\s*([A-Za-z0-9_]+)\s*\)", cbody): ifs.append(cm.group(2))
            cspp.append({'if_bridges':ifs})
        res.append((start,{'re_name':re_name,'base':base,'cspp_blocks':cspp}))
    return res

def parse_vqfx_chassis(text: str):
    res=[]
    for m in re.finditer(r"VQFX_CHASSIS_START\(\)\s*(?P<body>.*?)\s*VQFX_CHASSIS_END", text, flags=re.DOTALL):
        start=m.start(); body=m.group('body'); chname=nearest_define(text,start,'VQFX_CHASSIS_NAME') or f"vqfx_chassis_{len(res)+1}"
        pfe=[]
        for mm in re.finditer(r"(?:VQFX_PFE|COSIM)_START\((?P<label>[^,\)]+)(?:,(?P<idx>\d+))?\)\s*(?P<pbody>.*?)\s*(?:VQFX_PFE|COSIM)_END", body, flags=re.DOTALL):
            idx=int(mm.group('idx') or 0)
            pfe.append({'index': idx})
        ge=[]
        for cm in re.finditer(r"(?:VQFX_CONNECT|VMX_CONNECT)\(\s*GE\(\s*0\s*,\s*0\s*,\s*(\d+)\s*\)\s*,\s*([A-Za-z0-9_]+)\s*\)", body):
            ge.append(cm.group(2))
        if not pfe:
            pfe=[{'index':0}]
        res.append((start,{'name':chname,'ge_bridges':ge,'pfes':sorted(pfe,key=lambda x:x['index'])}))
    return res

def parse_vsrx_chassis(text: str):
    res=[]
    seen=set()
    for m in re.finditer(r"VSRX_CHASSIS_START\(\)\s*(?P<body>.*?)\s*VSRX_CHASSIS_END", text, flags=re.DOTALL):
        start=m.start(); body=m.group('body'); name=nearest_define(text,start,'VSRX_CHASSIS_NAME') or nearest_define(text,start,'VSRX_NAME') or f"vsrx_chassis_{len(res)+1}"
        ge=[]
        for cm in re.finditer(r"(?:VSRX_CONNECT|VQFX_CONNECT|VMX_CONNECT)\(\s*GE\(\s*0\s*,\s*0\s*,\s*(\d+)\s*\)\s*,\s*([A-Za-z0-9_]+)\s*\)", body):
            ge.append(cm.group(2))
        res.append((start,{'name':name,'ge_bridges':ge}))
        seen.add(name)
    for m in re.finditer(r"VSRX_RE_START\(\s*(?P<name>[^,\s\)]+)(?:\s*,\s*\d+)?\s*\)\s*(?P<body>.*?)\s*VSRX_RE_END", text, flags=re.DOTALL):
        start=m.start(); name=m.group('name')
        if name in seen:
            continue
        body=m.group('body'); ge=[]
        for cm in re.finditer(r"(?:VSRX_CONNECT|VQFX_CONNECT|VMX_CONNECT)\(\s*GE\(\s*0\s*,\s*0\s*,\s*(\d+)\s*\)\s*,\s*([A-Za-z0-9_]+)\s*\)", body):
            ge.append(cm.group(2))
        res.append((start,{'name':name,'ge_bridges':ge}))
        seen.add(name)
    return res

def render_args_template(template: str|None, vmid: int, re_vmid: int|None=None)->str|None:
    if not template:
        return None
    out=template.replace('<VMID>', str(vmid))
    out=out.replace('<RE-VMID>', str(re_vmid if re_vmid is not None else vmid))
    return out

def emit_qmconf(vm: VM, mac_prefix: str, used:set[str])->str:
    L=[f"cores: {vm.cores}","cpu: host",f"memory: {vm.memory}",f"name: {vm.name}",f"machine: {vm.machine}","ostype: l26","sockets: 1","serial0: socket"]
    if vm.conf_args:
        L.append(f"args: {vm.conf_args}")
    if vm.scsihw:
        L.append(f"scsihw: {vm.scsihw}")
    if vm.machine.startswith('pc-'): L.append('numa: 0')
    net0_mac=None
    first_mac=None
    for nic in sorted(vm.nics,key=lambda n:n.net):
        mac=mac_for(mac_prefix, vm.name, vm.vmid, nic.net, used)
        if first_mac is None:
            first_mac=mac
        if nic.net==0:
            net0_mac=mac
        entry=f"net{nic.net}: {'virtio' if nic.model=='virtio' else 'e1000'}={mac},bridge={nic.bridge}"
        if nic.vlan is not None: entry+=f",tag={nic.vlan}"
        L.append(entry)

    if vm.vmtype in ('vptx-re','vptx-cspp','vqfx-re'):
        mac_for_uuid=net0_mac or first_mac
        if mac_for_uuid:
            octets=mac_for_uuid.split(':')
            if len(octets)==6:
                mac_tail=''.join(octets[3:]).lower()
                uuid_suffix=f"{vm.vmid:04d}00{mac_tail}"
                L.append(f"smbios1: uuid=dbf05cba-68f9-465f-8507-{uuid_suffix}")
    return "\n".join(L)+"\n"

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--vmm',required=True)
    ap.add_argument('--type-registry',required=True)
    ap.add_argument('--overrides',required=False)
    ap.add_argument('--out',required=True)
    a=ap.parse_args()

    os.makedirs(a.out, exist_ok=True)

    # Copy the source VMM config to the canonical location consumed by scripts/ and Terraform.
    CANONICAL_VMM_PATH.parent.mkdir(parents=True, exist_ok=True)
    src_vmm=Path(a.vmm).resolve()
    dst_vmm=CANONICAL_VMM_PATH.resolve()
    if src_vmm != dst_vmm:
        shutil.copy(src_vmm, dst_vmm)

    reg=load_yaml(Path(a.type_registry)); ov=load_yaml(Path(a.overrides)) if a.overrides and Path(a.overrides).exists() else {}
    beh=reg.get('behavior') or {}
    text_raw=Path(a.vmm).read_text(errors='ignore')
    text=strip_comments(text_raw)
    disk_map=parse_basedisk_defines(text)

    regular=parse_regular_vms(text); vmx=parse_vmx_chassis(text); vqfx=parse_vqfx_chassis(text); vptx=parse_vptx_chassis(text); vsrx=parse_vsrx_chassis(text)
    entities=[]; entities += [(p,'regular',v) for p,v in regular]; entities += [(p,'vmx',c) for p,c in vmx]; entities += [(p,'vqfx',c) for p,c in vqfx]; entities += [(p,'vptx',c) for p,c in vptx]; entities += [(p,'vsrx',c) for p,c in vsrx]; entities.sort(key=lambda x:x[0])

    vlan_base=int((beh.get('vlan_policy') or {}).get('base',1010)); vlan_step=int((beh.get('vlan_policy') or {}).get('step',10))
    family_order=list((beh.get('vlan_policy') or {}).get('order') or ['vmx','vqfx','vptx','vsrx'])
    family_names={
        'vmx':[c['name'] for _,t,c in entities if t=='vmx'],
        'vqfx':[c['name'] for _,t,c in entities if t=='vqfx'],
        'vptx':[c['re_name'] for _,t,c in entities if t=='vptx'],
        'vsrx':[c['name'] for _,t,c in entities if t=='vsrx'],
    }
    family_vlans={k:{} for k in family_names}
    next_vlan=vlan_base
    ordered=family_order+[k for k in family_names if k not in family_order]
    for fam in ordered:
        names=family_names.get(fam) or []
        family_vlans[fam]={n:next_vlan+vlan_step*i for i,n in enumerate(names)}
        next_vlan+=vlan_step*len(names)
    vmx_vlan=family_vlans.get('vmx') or {}
    vqfx_vlan=family_vlans.get('vqfx') or {}
    vptx_vlan=family_vlans.get('vptx') or {}
    vsrx_vlan=family_vlans.get('vsrx') or {}

    vmid=int(beh.get('vmid_start',6000)); mac_prefix=beh.get('mac_prefix','BC:24:22')
    out=Path(a.out); qm_dir=out/'proxmox'/'qemu-server'; plan_dir=out/'plan'; rep_dir=out/'reports'; qm_dir.mkdir(parents=True, exist_ok=True); plan_dir.mkdir(parents=True, exist_ok=True); rep_dir.mkdir(parents=True, exist_ok=True)

    used=set(); vms=[]; unknown=[]; tokens=reg.get('tokens') or {}; types=reg.get('types') or {}
    bridge_symbols=set()
    for _,t,obj in entities:
        if t=='regular':
            for it in obj.get('interfaces') or []:
                if isinstance(it, dict):
                    if it.get('bridge'):
                        bridge_symbols.add(it['bridge'])
                elif it:
                    bridge_symbols.add(it)
        elif t=='vmx':
            for m in obj.get('mpcs') or []: bridge_symbols.update(m.get('ge_bridges') or [])
        elif t=='vqfx':
            bridge_symbols.update(obj.get('ge_bridges') or [])
        elif t=='vptx':
            for blk in obj.get('cspp_blocks') or []: bridge_symbols.update(blk.get('if_bridges') or [])
        elif t=='vsrx':
            bridge_symbols.update(obj.get('ge_bridges') or [])
    bridge_map=build_bridge_map(bridge_symbols)

    regular_vqfx_re=[]
    regular_vqfx_pfe=[]

    for _,t,obj in entities:
        if t=='regular':
            vm=obj; token=resolve_regular_token(vm, tokens, disk_map)
            vm['token']=token
            vtype=(ov.get('vm_overrides') or {}).get(vm['name']) or (ov.get('token_overrides') or {}).get(token) or (tokens.get(token) if token else None)
            if vtype=='vqfx' and token=='COSIM_DISK':
                vtype='vqfx-pfe'

            if vtype not in ('linux','modem','vqfx','vqfx-pfe','vsrx'):
                unknown.append({'vm':vm['name'],'token':token,'reason':'unknown-or-unsupported'}); continue

            if vtype=='vqfx':
                regular_vqfx_re.append(vm)
                continue
            if vtype=='vqfx-pfe':
                regular_vqfx_pfe.append(vm)
                continue
            if vtype=='vsrx':
                tdef=types.get('vsrx') or {}
                re=tdef.get('re') or {}
                model=re.get('nic_model', tdef.get('nic_model','e1000'))
                machine=tdef.get('machine','pc-i440fx-7.0')
                mgmt_bridge=(tdef.get('bridges') or {}).get('mgmt','vmbr_mgmt')
                iface_map={int(it.get('ifindex')):it for it in (vm.get('interfaces') or []) if isinstance(it, dict) and (it.get('bridge') or it.get('external'))}
                nics=[]
                for ifidx,it in sorted(iface_map.items()):
                    bn=it.get('bridge')
                    if it.get('external') and not bn:
                        nics.append(Nic(ifidx, model, mgmt_bridge))
                        continue
                    nics.append(Nic(ifidx, model, bridge_map[bn]))
                cores=int(re.get('cores',2))
                memory=int(re.get('memory_mb',4096))
                expected=None
                for cand in token_candidates(token):
                    expected=disk_map.get(cand)
                    if expected:
                        break
                if not expected:
                    expected=(ov.get('default_images') or {}).get('vsrx')
                vms.append(VM(vmid, proxmox_vm_name(vm['name']), 'vsrx-re', machine, cores, memory, nics, expected_image=expected, boot_after_attach='order=ide0', conf_args=render_args_template(re.get('conf_args'), vmid) or "-machine accel=kvm:tcg -cpu host"))
                vmid+=1
                continue

            tdef=types.get(vtype) or {}
            nics=[Nic(i,tdef.get('nic_model','virtio'),bridge_map[it['bridge']]) for i,it in enumerate([x for x in (vm.get('interfaces') or []) if isinstance(x, dict) and x.get('bridge')])]

            reg_cores=int(tdef.get('cores') or 1)
            reg_memory=int(tdef.get('memory_mb') or tdef.get('memory') or 1024)

            if vtype=='modem':
                cores=reg_cores
                memory=reg_memory
            else:
                cores=int(vm.get('ncpus')) if vm.get('ncpus') is not None else reg_cores
                memory=int(vm.get('memory')) if vm.get('memory') is not None else reg_memory

            expected=None
            for cand in token_candidates(token):
                expected=disk_map.get(cand)
                if expected:
                    break
            vms.append(VM(vmid, proxmox_vm_name(vm['name']), vtype, tdef.get('machine','q35'), cores, memory, nics, expected_image=expected, boot_after_attach='order=ide0'))
            vmid+=1

    if regular_vqfx_re:
        tdef=types.get('vqfx') or {}
        br=tdef.get('bridges') or {}
        mgmt=br.get('mgmt','vmbr_mgmt'); fpc=br.get('fpc','vmbr6_fpc')
        re=tdef.get('re') or {}; pfe=tdef.get('pfe') or {}
        machine=tdef.get('machine','pc-i440fx-7.0')
        used_pfe=set()
        allocated_vlans=[v for fam in family_vlans.values() for v in fam.values()]
        next_dynamic_vlan=(max(allocated_vlans)+vlan_step) if allocated_vlans else vlan_base

        def vm_bridges(v):
            return {it.get('bridge') for it in (v.get('interfaces') or []) if isinstance(it, dict) and it.get('bridge')}

        for re_vm in regular_vqfx_re:
            re_br=vm_bridges(re_vm)
            pfe_vm=None
            for idx,cand in enumerate(regular_vqfx_pfe):
                if idx in used_pfe:
                    continue
                if re_br.intersection(vm_bridges(cand)):
                    pfe_vm=cand; used_pfe.add(idx); break
            if pfe_vm is None:
                for idx,cand in enumerate(regular_vqfx_pfe):
                    if idx in used_pfe:
                        continue
                    pfe_vm=cand; used_pfe.add(idx); break

            base=next_dynamic_vlan
            next_dynamic_vlan+=vlan_step
            vqfx_vlan[re_vm['name']]=base

            iface_map={int(it.get('ifindex')):it.get('bridge') for it in (re_vm.get('interfaces') or []) if isinstance(it, dict) and it.get('bridge')}
            re_nics=[
                Nic(0,re.get('nic_model','e1000'),mgmt),
                Nic(1,re.get('nic_model','e1000'),fpc,base),
                Nic(2,re.get('nic_model','e1000'),fpc,base),
            ]
            for ifidx in sorted(iface_map):
                if ifidx<3:
                    continue
                bn=iface_map[ifidx]
                re_nics.append(Nic(ifidx,re.get('nic_model','e1000'),bridge_map[bn]))

            re_token=resolve_regular_token(re_vm, tokens, disk_map)
            re_expected=None
            for cand in token_candidates(re_token):
                re_expected=disk_map.get(cand)
                if re_expected:
                    break
            if not re_expected:
                re_expected=(ov.get('default_images') or {}).get('vqfx_re')

            re_cores=int(re.get('cores',1))
            re_mem=int(re.get('memory_mb',1200))
            re_vmid=vmid
            vms.append(VM(vmid, proxmox_vm_name(re_vm['name'])+'-re', 'vqfx-re', machine, re_cores, re_mem, re_nics, expected_image=re_expected, boot_after_attach='order=ide0', conf_args=render_args_template(re.get('conf_args'), vmid) or "-machine accel=kvm:tcg -cpu host"))
            vmid+=1

            pfe_nics=[
                Nic(0,pfe.get('nic_model','e1000'),mgmt),
                Nic(1,pfe.get('nic_model','e1000'),fpc,base),
                Nic(2,pfe.get('nic_model','e1000'),fpc,base),
            ]
            pfe_expected=(ov.get('default_images') or {}).get('vqfx_pfe')
            pfe_cores=int(pfe.get('cores',1)); pfe_mem=int(pfe.get('memory_mb',1024))
            pfe_name=proxmox_vm_name(re_vm['name'])+'-pfe0'
            if pfe_vm:
                pfe_name=proxmox_vm_name(pfe_vm['name'])
                pfe_token=resolve_regular_token(pfe_vm, tokens, disk_map)
                for cand in token_candidates(pfe_token):
                    pfe_expected=disk_map.get(cand) or pfe_expected
                    if disk_map.get(cand):
                        break

            vms.append(VM(vmid, pfe_name, 'vqfx-pfe', machine, pfe_cores, pfe_mem, pfe_nics, expected_image=pfe_expected, boot_after_attach='order=ide0', conf_args=render_args_template(pfe.get('conf_args'), vmid, re_vmid) or "-machine accel=kvm:tcg -cpu host"))
            vmid+=1

    for _,t,obj in entities:
        if t=='vmx':
            ch=obj; base=vmx_vlan[ch['name']]; tdef=types.get('vmx') or {}; br=tdef.get('bridges') or {}; mgmt=br.get('mgmt','vmbr_mgmt'); fpc=br.get('fpc','vmbr6_fpc'); re=tdef.get('re') or {}; mpc=tdef.get('mpc') or {}; machine=tdef.get('machine','pc-i440fx-7.0')
            re_vmid=vmid
            re_name=proxmox_vm_name(ch['name'])+'-re'; vms.append(VM(vmid,re_name,'vmx-re',machine,int(re.get('cores',1)),int(re.get('memory_mb',1230)),[Nic(0,re.get('nic_model','e1000'),mgmt),Nic(1,re.get('nic_model','e1000'),fpc,base)],expected_image=disk_map.get('VMX_DISK'),extra_ide1=re.get('extra_disk_ide1'),boot_after_attach='order=ide0',conf_args=render_args_template(re.get('conf_args'), vmid, re_vmid) or f"-machine accel=kvm:tcg -smbios type=1,product=VM-{re_vmid}-48-re-0 -cpu 'host,kvm=on'",scsihw=re.get('scsihw','virtio-scsi-pci')))
            vmid+=1; ge_off=int(mpc.get('ge_net_offset',2))
            mpc_expected=(ov.get('default_images') or {}).get('vmx_mpc')
            for m in ch.get('mpcs') or []:
                mname=proxmox_vm_name(ch['name'])+f"-mpc{int(m['index'])}"; nics=[Nic(0,mpc.get('nic_model','virtio'),mgmt),Nic(1,mpc.get('nic_model','virtio'),fpc,base)]
                for i,bn in enumerate(m.get('ge_bridges') or []): nics.append(Nic(i+ge_off, mpc.get('nic_model','virtio'), bridge_map[bn]))
                vms.append(VM(vmid,mname,'vmx-mpc',machine,int(mpc.get('cores',3)),int(mpc.get('memory_mb',2250)),nics,expected_image=mpc_expected,boot_after_attach='order=ide0',conf_args=render_args_template(mpc.get('conf_args'), vmid, re_vmid) or f"-machine accel=kvm:tcg -smbios type=1,product=VM-{re_vmid}-48-mpc-0 -cpu host"))
                vmid+=1
        elif t=='vqfx':
            ch=obj; base=vqfx_vlan[ch['name']]; tdef=types.get('vqfx') or {}; br=tdef.get('bridges') or {}; mgmt=br.get('mgmt','vmbr_mgmt'); fpc=br.get('fpc','vmbr6_fpc'); re=tdef.get('re') or {}; pfe=tdef.get('pfe') or {}; machine=tdef.get('machine','pc-i440fx-7.0')
            ge_off=max(3, int(re.get('ge_net_offset',2)))
            re_nics=[Nic(0,re.get('nic_model','e1000'),mgmt),Nic(1,re.get('nic_model','e1000'),fpc,base),Nic(2,re.get('nic_model','e1000'),fpc,base)]
            for i,bn in enumerate(ch.get('ge_bridges') or []):
                re_nics.append(Nic(i+ge_off,re.get('nic_model','e1000'),bridge_map[bn]))
            re_expected=disk_map.get('VQFX10_DISK') or (ov.get('default_images') or {}).get('vqfx_re')
            vms.append(VM(vmid,proxmox_vm_name(ch['name'])+'-re','vqfx-re',machine,int(re.get('cores',1)),int(re.get('memory_mb',1200)),re_nics,expected_image=re_expected,boot_after_attach='order=ide0',conf_args=render_args_template(re.get('conf_args'), vmid) or "-machine accel=kvm:tcg -cpu host"))
            vmid+=1
            pfe_expected=disk_map.get('COSIM_DISK') or (ov.get('default_images') or {}).get('vqfx_pfe')
            pfe_blocks=ch.get('pfes') or [{'index':0}]
            for blk in pfe_blocks:
                idx=int(blk.get('index',0))
                pfe_name=proxmox_vm_name(ch['name'])+f"-pfe{idx}"
                pfe_nics=[Nic(0,pfe.get('nic_model','e1000'),mgmt),Nic(1,pfe.get('nic_model','e1000'),fpc,base),Nic(2,pfe.get('nic_model','e1000'),fpc,base)]
                vms.append(VM(vmid,pfe_name,'vqfx-pfe',machine,int(pfe.get('cores',1)),int(pfe.get('memory_mb',1024)),pfe_nics,expected_image=pfe_expected,boot_after_attach='order=ide0',conf_args=render_args_template(pfe.get('conf_args'), vmid) or "-machine accel=kvm:tcg -cpu host"))
                vmid+=1
        elif t=='vptx':
            ch=obj; base=vptx_vlan[ch['re_name']]; tdef=types.get('vptx') or {}; machine=tdef.get('machine','pc-i440fx-7.0'); re=tdef.get('re') or {}; cspp=tdef.get('cspp') or {}
            cspp_expected=(ov.get('default_images') or {}).get('vptx_cspp')
            nics=[]
            for it in re.get('fixed_nics') or []:
                vlan=None
                if it.get('vlan')=='base': vlan=base
                elif it.get('vlan')=='base_plus_1': vlan=base+1
                nics.append(Nic(int(it['net']), re.get('nic_model','virtio'), it['bridge'], vlan))
            vms.append(VM(vmid,proxmox_vm_name(ch['re_name']),'vptx-re',machine,int(re.get('cores',4)),int(re.get('memory_mb',4096)),nics,extra_ide0=re.get('extra_disk_ide0'),iso_name=disk_map.get('EVOVPTX_DISK1'),boot_after_attach=re.get('boot_order_after_attach','order=ide0;ide2'),conf_args=render_args_template(re.get('conf_args'), vmid) or "-machine accel=kvm:tcg -smbios 'type=0,vendor=Bochs,version=Bochs' -smbios 'type=3,manufacturer=Bochs' -smbios 'type=1,manufacturer=Bochs,product=Bochs,serial=chassis_no=0:slot=0:type=1:assembly_id=0x0CDC:platform=220:master=0' -cpu host",scsihw=re.get('scsihw','virtio-scsi-pci')))
            vmid+=1
            blocks=(ch.get('cspp_blocks') or [])
            if ( (reg.get('behavior') or {}).get('vptx_cspp_mode','single').lower()=='single' ):
                blocks=blocks[:1]
            if not blocks: blocks=[{'if_bridges':[]}]
            if_cfg=cspp.get('if_ports') or {}; net_off=int(if_cfg.get('net_offset',4)); model=if_cfg.get('model','e1000')
            for idx,blk in enumerate(blocks):
                name=proxmox_vm_name(ch['base'])+f"-cspp{idx}"; nics=[]
                for it in cspp.get('fixed_nics') or []:
                    vlan=None
                    if it.get('vlan')=='base': vlan=base
                    elif it.get('vlan')=='base_plus_1': vlan=base+1
                    nics.append(Nic(int(it['net']), it.get('model','virtio'), it['bridge'], vlan))
                for i,bn in enumerate(blk.get('if_bridges') or []): nics.append(Nic(i+net_off, model, bridge_map[bn]))
                vms.append(VM(vmid,name,'vptx-cspp',machine,int(cspp.get('cores',3)),int(cspp.get('memory_mb',3280)),nics,expected_image=cspp_expected,boot_after_attach=cspp.get('boot_order_after_attach','order=ide0'),conf_args=render_args_template(cspp.get('conf_args'), vmid) or "-machine accel=kvm:tcg -smbios 'type=1,product=0,serial=/slotid=1/assembly=0/pic=0@0' -cpu host"))
                vmid+=1
        elif t=='vsrx':
            ch=obj; tdef=types.get('vsrx') or {}; re=tdef.get('re') or {}; machine=tdef.get('machine','pc-i440fx-7.0')
            nics=[]
            for i,bn in enumerate(ch.get('ge_bridges') or []):
                nics.append(Nic(i,re.get('nic_model','e1000'),bridge_map[bn]))
            expected=disk_map.get('VSRX_DISK') or (ov.get('default_images') or {}).get('vsrx')
            vms.append(VM(vmid,proxmox_vm_name(ch['name'])+'-re','vsrx-re',machine,int(re.get('cores',2)),int(re.get('memory_mb',4096)),nics,expected_image=expected,boot_after_attach='order=ide0',conf_args=render_args_template(re.get('conf_args'), vmid) or "-machine accel=kvm:tcg -cpu host"))
            vmid+=1

    for vm in vms: ( (out/'proxmox'/'qemu-server')/f"{vm.vmid}.conf" ).write_text(emit_qmconf(vm, mac_prefix, used))
    plan_storage_id=beh.get('storage_id') or 'local-lvm'
    plan={'image_dir': beh.get('image_dir','/root/import'), 'storage_id': plan_storage_id, 'iso_storage_id': beh.get('iso_storage_id','local'), 'iso_storage_path': beh.get('iso_storage_path','/var/lib/vz/template/iso'), 'interactive': bool(beh.get('interactive',False)), 'unknown_action': beh.get('unknown_action','skip'), 'vms':[]}
    for vm in vms: plan['vms'].append({'vmid':vm.vmid,'name':vm.name,'vmtype':vm.vmtype,'boot_after_attach':vm.boot_after_attach,'expected_image':vm.expected_image,'extra_ide1':vm.extra_ide1,'extra_ide0':vm.extra_ide0,'iso_name':vm.iso_name,'storage_id':plan_storage_id})
    ( (out/'plan')/'attach_plan.json').write_text(json.dumps(plan, indent=2))
    ( (out/'reports')/'unknowns.json').write_text(json.dumps(unknown, indent=2))
    print(f"Generated {len(vms)} VM config(s)")

if __name__=='__main__': main()
