"""Refresh owner VPN subscription; keep last valid configuration on failure."""
import base64
import json
import os
from pathlib import Path
import subprocess
import urllib.parse
import urllib.request

ROOT = Path('/etc/sing-box')

def main():
    url = (ROOT / 'subscription.url').read_text().strip()
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'sing-box'}), timeout=30) as response:
        raw = response.read(2_000_000)
    text = raw.decode()
    if '://' not in text:
        text = base64.b64decode(raw + b'===').decode()
    nodes = []
    skipped = 0
    for line in text.splitlines():
        if '://' not in line:
            continue
        u = urllib.parse.urlsplit(line.strip())
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        transport = q.get('type', 'tcp')
        if u.scheme not in {'vless', 'hysteria2', 'hy2'} or transport not in {'tcp', 'grpc', 'ws', 'httpupgrade'}:
            skipped += 1
            continue
        node = {'type': 'hysteria2' if u.scheme == 'hy2' else u.scheme, 'tag': f'vpn-{len(nodes)+1:02}', 'server': u.hostname, 'server_port': u.port or 443, 'connect_timeout': '8s'}
        if node['type'] == 'vless':
            node['uuid'] = urllib.parse.unquote(u.username or '')
            if q.get('flow'):
                node['flow'] = q['flow']
        else:
            node['password'] = urllib.parse.unquote(u.username or '')
        security = q.get('security', 'tls' if node['type'] == 'hysteria2' else 'none')
        if security in {'tls', 'reality'}:
            tls = {'enabled': True, 'server_name': q.get('sni', u.hostname)}
            if q.get('alpn'):
                tls['alpn'] = q['alpn'].split(',')
            if q.get('fp'):
                tls['utls'] = {'enabled': True, 'fingerprint': q['fp']}
            if security == 'reality':
                tls['reality'] = {'enabled': True, 'public_key': q['pbk'], 'short_id': q.get('sid', '')}
            node['tls'] = tls
        if transport == 'grpc':
            node['transport'] = {'type': 'grpc', 'service_name': q.get('serviceName', '')}
        elif transport in {'ws', 'httpupgrade'}:
            node['transport'] = {'type': transport, 'path': q.get('path', '/')}
            if q.get('host'):
                node['transport']['headers' if transport == 'ws' else 'host'] = {'Host': q['host']} if transport == 'ws' else q['host']
        nodes.append(node)
    if not nodes:
        raise RuntimeError('No compatible nodes; keeping existing config')
    config_path = ROOT / 'config.json'
    previous = config_path.read_bytes()
    config = json.loads(previous)
    config['outbounds'] = [{'type': 'urltest', 'tag': 'proxy', 'outbounds': [x['tag'] for x in nodes], 'url': 'https://api.telegram.org/', 'interval': '30s', 'tolerance': 500, 'idle_timeout': '24h', 'interrupt_exist_connections': False}] + nodes
    payload = (json.dumps(config, indent=2) + '\n').encode()
    if payload == previous:
        print('VPN subscription unchanged')
        return
    temporary = ROOT / 'config.next'
    temporary.write_bytes(payload)
    temporary.chmod(0o600)
    result = subprocess.run(['sing-box', 'check', '-c', str(temporary)], capture_output=True)
    if result.returncode:
        raise RuntimeError('VPN config validation failed; keeping existing config')
    backup = ROOT / 'config.last-working.backup'
    backup.write_bytes(previous)
    backup.chmod(0o600)
    os.replace(temporary, config_path)
    result = subprocess.run(['systemctl', 'restart', 'sing-box'], capture_output=True)
    if result.returncode:
        config_path.write_bytes(previous)
        subprocess.run(['systemctl', 'restart', 'sing-box'], capture_output=True)
        raise RuntimeError('VPN restart failed; previous configuration restored')
    print(f'VPN updated: {len(nodes)} nodes, {skipped} unsupported transports skipped')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('VPN refresh failed:', type(exc).__name__)
        raise SystemExit(1)
