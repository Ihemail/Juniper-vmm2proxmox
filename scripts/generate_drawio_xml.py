import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VMM_FILE = REPO_ROOT / 'output' / 'input.vmm'


def _clean_bridge_name(raw: str) -> str:
    return raw.strip().strip('"').strip().rstrip(');').strip()


def parse_vmm_config(vmm_text: str):
    node_to_bridges = {}
    current_node = None
    in_vm_block = False

    for raw_line in vmm_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('//'):
            continue

        vm_start = re.search(r'^vm\s+"([^"]+)"\s*\{', line)
        if vm_start:
            current_node = vm_start.group(1)
            in_vm_block = True
            node_to_bridges.setdefault(current_node, set())
            continue

        vmx_re_start = re.search(r'^VMX_RE_START\(([^,\s\)]+)', line)
        if vmx_re_start:
            current_node = vmx_re_start.group(1)
            node_to_bridges.setdefault(current_node, set())
            continue

        evovptx_start = re.search(r'^EVOVPTX_CHASSIS_START_\s*\(([^\)\s]+)', line)
        if evovptx_start:
            current_node = evovptx_start.group(1)
            node_to_bridges.setdefault(current_node, set())
            continue

        if in_vm_block and line.startswith('};'):
            in_vm_block = False
            current_node = None
            continue

        if in_vm_block:
            vm_bridge = re.search(r'bridge\s+"([^"]+)"', line)
            if vm_bridge and current_node:
                node_to_bridges[current_node].add(vm_bridge.group(1))
            continue

        connect_match = re.search(r'^(?:VMX_CONNECT|EVOVPTX_CONNECT)\s*\(.*?,\s*([^\)]+)\)', line)
        if connect_match and current_node:
            bridge_name = _clean_bridge_name(connect_match.group(1))
            if bridge_name:
                node_to_bridges[current_node].add(bridge_name)

    parsed = []
    for node_name in sorted(node_to_bridges.keys()):
        bridges = sorted(node_to_bridges[node_name])
        parsed.append((node_name, bridges))
    return parsed


def build_drawio_xml(topology):
    mxfile = ET.Element('mxfile', host='app.diagrams.net', version='22.1.16')
    diagram = ET.SubElement(mxfile, 'diagram', id='topology', name='Page-1')
    graph = ET.SubElement(
        diagram,
        'mxGraphModel',
        dx='1600',
        dy='900',
        grid='1',
        gridSize='10',
        guides='1',
        tooltips='1',
        connect='1',
        arrows='1',
        fold='1',
        page='1',
        pageScale='1',
        pageWidth='1920',
        pageHeight='1080',
        math='0',
        shadow='0',
    )
    root = ET.SubElement(graph, 'root')
    ET.SubElement(root, 'mxCell', id='0')
    ET.SubElement(root, 'mxCell', id='1', parent='0')

    next_id = 2
    node_cell_ids = {}
    bridge_cell_ids = {}

    node_x = 40
    node_y = 40
    node_col_width = 260
    node_row_height = 90

    for index, (node_name, _) in enumerate(topology):
        cell_id = str(next_id)
        next_id += 1
        node_cell_ids[node_name] = cell_id
        x = node_x + (index % 5) * node_col_width
        y = node_y + (index // 5) * node_row_height
        cell = ET.SubElement(
            root,
            'mxCell',
            id=cell_id,
            value=node_name,
            style='rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;',
            vertex='1',
            parent='1',
        )
        ET.SubElement(cell, 'mxGeometry', x=str(x), y=str(y), width='200', height='48', attrib={'as': 'geometry'})

    bridge_x = 40
    bridge_y = 620
    bridge_col_width = 200
    bridge_row_height = 70

    unique_bridges = sorted({bridge for _, bridges in topology for bridge in bridges})
    for index, bridge_name in enumerate(unique_bridges):
        cell_id = str(next_id)
        next_id += 1
        bridge_cell_ids[bridge_name] = cell_id
        x = bridge_x + (index % 7) * bridge_col_width
        y = bridge_y + (index // 7) * bridge_row_height
        cell = ET.SubElement(
            root,
            'mxCell',
            id=cell_id,
            value=bridge_name,
            style='ellipse;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;',
            vertex='1',
            parent='1',
        )
        ET.SubElement(cell, 'mxGeometry', x=str(x), y=str(y), width='150', height='42', attrib={'as': 'geometry'})

    for node_name, bridges in topology:
        source_id = node_cell_ids[node_name]
        for bridge_name in bridges:
            target_id = bridge_cell_ids.get(bridge_name)
            if not target_id:
                continue
            edge = ET.SubElement(
                root,
                'mxCell',
                id=str(next_id),
                edge='1',
                parent='1',
                source=source_id,
                target=target_id,
                style='endArrow=none;html=1;strokeColor=#999999;',
            )
            next_id += 1
            ET.SubElement(edge, 'mxGeometry', relative='1', attrib={'as': 'geometry'})

    return ET.tostring(mxfile, encoding='unicode')


def main():
    parser = argparse.ArgumentParser(description='Generate draw.io XML import file from VMM configuration.')
    parser.add_argument(
        '--vmm-file',
        default=str(DEFAULT_VMM_FILE),
        help='Path to the canonical VMM configuration file (defaults to ./output/input.vmm)',
    )
    parser.add_argument('vmm_config_file', nargs='?', help='Optional override path to a VMM configuration file')
    parser.add_argument('--out', help='Optional output XML path. If omitted, XML is printed to stdout.')
    args = parser.parse_args()

    vmm_path = Path(args.vmm_config_file or args.vmm_file).expanduser()
    if not vmm_path.is_file():
        raise SystemExit(f'VMM configuration not found at {vmm_path}')

    vmm_config = vmm_path.read_text(encoding='utf-8', errors='ignore')
    topology = parse_vmm_config(vmm_config)
    if not topology:
        raise SystemExit('No topology data found in VMM input.')

    xml_str = build_drawio_xml(topology)

    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(xml_str, encoding='utf-8')
        print(f'Wrote draw.io XML to {out_path}')
    else:
        print(xml_str)


if __name__ == '__main__':
    main()