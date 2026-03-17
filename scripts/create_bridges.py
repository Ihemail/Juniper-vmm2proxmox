#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
try:
    import yaml
except Exception:
    yaml=None
try:
    import paramiko
except Exception:
    paramiko=None

REPO_ROOT=Path(__file__).resolve().parents[1]
DEFAULT_VMM_FILE=REPO_ROOT/'output'/'input.vmm'

def die(msg: str, code: int=1): print(f"ERROR: {msg}", file=sys.stderr); raise SystemExit(code)

def load_yaml(p: Path)->dict:
    if yaml is None: die('pyyaml required: pip3 install pyyaml')
    return yaml.safe_load(p.read_text()) or {}

def normalize(name: str)->str: return re.sub(r"[^a-z0-9]", "", name.lower())

def br_name(vmm_bridge: str)->str: return f"vmc_{normalize(vmm_bridge)[:6]}"

def strip_comments(text: str)->str: return re.sub(r"//.*$", "", text, flags=re.MULTILINE)

def build_targets(vmm_bridges: set[str])->list[str]:
    mapped={}
    for src in sorted(vmm_bridges):
        dst=br_name(src)
        other=mapped.get(dst)
        if other is not None and other!=src:
            die(f"Bridge name collision after truncation: {src} and {other} -> {dst}")
        mapped[dst]=src
    return sorted(mapped.keys())

def ssh_run(cfg: dict, cmd: str, check: bool=True):
    if paramiko is None:
        die('paramiko required: pip3 install paramiko')
    print(f"+ ssh {cfg['host']}: {cmd}")
    client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(cfg['host'], port=cfg.get('ssh_port',22), username=cfg['ssh_user'], key_filename=cfg['ssh_private_key_path'])
    _stdin, stdout, stderr=client.exec_command(cmd)
    returncode=stdout.channel.recv_exit_status()
    out=stdout.read().decode(); err=stderr.read().decode()
    client.close()
    class Result: pass
    res=Result(); res.returncode=returncode; res.stdout=out; res.stderr=err
    if check and returncode!=0:
        die(f"Remote command failed ({cmd}): {err or out}")
    return res

def parse_vmm_bridges(txt: str)->set[str]:
    s=set()
    for m in re.finditer(r"bridge\s+\"([^\"]+)\"\s*\{\s*\}\s*;", txt): s.add(m.group(1))
    for m in re.finditer(r"VMX_CONNECT\(\s*GE\(\s*0\s*,\s*0\s*,\s*\d+\s*\)\s*,\s*([A-Za-z0-9_]+)\s*\)", txt): s.add(m.group(1))
    for m in re.finditer(r"EVOVPTX_CONNECT\(\s*IF_ET\s*\(\s*[01]\s*,\s*0\s*,\s*\d+\s*\)\s*,\s*([A-Za-z0-9_]+)\s*\)", txt): s.add(m.group(1))
    return s

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--config',required=True)
    ap.add_argument('--vmm-file',default=str(DEFAULT_VMM_FILE),help='Path to the single VMM config file to parse (defaults to ./output/input.vmm)')
    ap.add_argument('--state-dir',required=True)
    ap.add_argument('--no-apply',action='store_true'); args=ap.parse_args()
    cfg=load_yaml(Path(args.config))['proxmox']
    raw=(args.vmm_file or str(DEFAULT_VMM_FILE)).strip() or str(DEFAULT_VMM_FILE)
    if raw and raw[0]==raw[-1] and raw[0] in '"\'': raw=raw[1:-1]
    vmm_path=Path(raw).expanduser()
    if not vmm_path.is_file(): die(f"VMM file not found: {vmm_path}")
    vmm_text=strip_comments(vmm_path.read_text())
    lines=vmm_text.splitlines()
    start_idx=0
    for i,line in enumerate(lines):
        if line.strip()=='## PRIVATE_BRIDGES ##':
            start_idx=i+1
            break
    bridges_section=lines[start_idx:]
    for i,line in enumerate(bridges_section):
        if line.strip()=='PRIVATE_BRIDGES':
            bridges_section=bridges_section[:i]
            break
    vmm_text="\n".join(bridges_section)
    state=Path(args.state_dir); state.mkdir(parents=True, exist_ok=True)
    exclude={'vmbr_mgmt','vmbr6_fpc'}
    targets=sorted(set(build_targets(parse_vmm_bridges(vmm_text)))-exclude)
    if not targets:
        (state/'created_bridges.json').write_text(json.dumps({'bridges':[],'newly_created':[]},indent=2)); print('No interconnection bridges found.'); return
    cp=ssh_run(cfg,"ip -o link show | awk -F': ' '{print $2}'",check=False)
    existing=set(cp.stdout.split()) if cp.returncode==0 else set(); missing=[b for b in targets if b not in existing]
    marker_start='# --- vmm2proxmox managed bridges (start)'; marker_end='# --- vmm2proxmox managed bridges (end)'
    ensure_markers_script=(
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        f"marker_start={marker_start!r}\n"
        f"marker_end={marker_end!r}\n"
        "p=Path('/etc/network/interfaces')\n"
        "text=p.read_text()\n"
        "if marker_start not in text or marker_end not in text:\n"
        "    if text and not text.endswith('\\n'):\n"
        "        text += '\\n'\n"
        "    text += f'\\n{marker_start}\\n{marker_end}\\n'\n"
        "    p.write_text(text)\n"
        "    print('[MARKER] Added managed bridge markers')\n"
        "PY"
    )
    ssh_run(cfg, ensure_markers_script, check=True)
    created=[]
    for br in missing:
        # Use Python script via SSH to add bridge configuration reliably
        add_bridge_script=(
            "python3 - <<'PYSCRIPT'\n"
            "from pathlib import Path\n"
            "import re\n"
            "br='" + br + "'\n"
            "p=Path('/etc/network/interfaces')\n"
            "text=p.read_text()\n"
            "if f'iface {br} inet' in text:\n"
            "    exit(0)\n"
            "stanza=f'auto {br}\\niface {br} inet manual\\n    bridge-ports none\\n    bridge-stp off\\n    bridge-fd 0\\n    mtu 9600\\n'\n"
            "# Find marker_end by regex to handle various formats (with or without proper newlines)\n"
            "pattern=r'# --- vmm2proxmox managed bridges \\(end\\)'\n"
            "match=re.search(pattern,text)\n"
            "if match:\n"
            "    pos=match.start()\n"
            "    text=text[:pos]+stanza+'\\n'+text[pos:]\n"
            "else:\n"
            "    text=text.rstrip()+'\\n\\n'+stanza\n"
            "p.write_text(text)\n"
            "print(f'[ADDED] Bridge {br} to /etc/network/interfaces')\n"
            "PYSCRIPT"
        )
        resp=ssh_run(cfg, add_bridge_script, check=True)
        if '[ADDED]' in (resp.stdout or ''):
            created.append(br)
    ensure_mtu_script=(
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "import re\n"
        f"targets={repr(targets)}\n"
        "p=Path('/etc/network/interfaces')\n"
        "text=p.read_text()\n"
        "for br in targets:\n"
        "    pat=re.compile(rf'(^iface\\s+{re.escape(br)}\\s+inet\\s+manual\\s*\\n)(?P<body>(?:^[ \\t].*\\n)*)', re.MULTILINE)\n"
        "    def repl(m):\n"
        "        body=m.group('body')\n"
        "        body=''.join(ln for ln in body.splitlines(True) if not re.match(r'^[ \\t]*mtu\\s+\\d+\\s*$', ln))\n"
        "        return m.group(1)+body+'    mtu 9600\\n'\n"
        "    text,_=pat.subn(repl, text, count=1)\n"
        "p.write_text(text)\n"
        "print('Ensured mtu 9600 for target vmc bridges')\n"
        "PY"
    )
    ssh_run(cfg, ensure_mtu_script)
    live_mtu_cmd="for b in " + " ".join(targets) + "; do ip link show \"$b\" >/dev/null 2>&1 && ip link set dev \"$b\" mtu 9600 || true; done"
    ssh_run(cfg, live_mtu_cmd, check=False)
    if not args.no_apply: ssh_run(cfg,'ifreload -a',check=False)
    previous_newly_created=[]
    state_json=state/'created_bridges.json'
    if state_json.exists():
        try:
            previous_payload=json.loads(state_json.read_text())
            previous_newly_created=previous_payload.get('newly_created') or []
        except Exception:
            previous_newly_created=[]
    merged_newly_created=sorted(set(previous_newly_created).union(created).intersection(set(targets)))
    (state/'created_bridges.json').write_text(json.dumps({'bridges':targets,'newly_created':merged_newly_created},indent=2))
    (state/'created_bridges.txt').write_text("\n".join(targets)+"\n")
    print(f"[STATE] Recorded {len(targets)} total bridges ({len(merged_newly_created)} newly created) to {state/'created_bridges.json'}")
if __name__=='__main__': main()
