"""Initialize a private Linux acceptance service; prints no credentials."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import argparse
import ipaddress
import shutil

parser = argparse.ArgumentParser()
parser.add_argument('--host', type=ipaddress.IPv4Address, required=True)
host = str(parser.parse_args().host)

root = Path('/etc/olivia-gpu')
root.mkdir(exist_ok=True)
os.umask(0o077)
config = root/'config.json'
if not config.exists():
    config.write_text(json.dumps({'public_url':'https://' + host,
        'signing_key':secrets.token_urlsafe(48),
        'tokens':{'acceptance-user-a':secrets.token_urlsafe(32),'acceptance-user-b':secrets.token_urlsafe(32)},
        'gpus':['0'],'profiles':{},'shared_assets':{}}),encoding='utf-8')
    config.chmod(0o600)
shutil.chown(config, user='ubuntu', group='ubuntu')
if not (root/'server.key').exists():
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-sha256','-nodes','-days','7',
        '-keyout',str(root/'server.key'),'-out',str(root/'server.crt'),
        '-subj','/CN=' + host,'-addext','subjectAltName=IP:' + host],check=True,capture_output=True)
    (root/'server.key').chmod(0o600)
(root/'server.crt').chmod(0o644)
service='''[Unit]
Description=Olivia GPU acceptance task API
After=network-online.target
[Service]
User=ubuntu
WorkingDirectory=/opt/olivia-gpu
ExecStart=/opt/olivia-gpu/.venv/bin/python -m gpu_service.server --config /etc/olivia-gpu/config.json --data /var/lib/olivia-gpu/tasks --port 18880
Restart=on-failure
UMask=0077
KillMode=control-group
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
'''
Path('/etc/systemd/system/olivia-gpu.service').write_text(service)
Path('/etc/nginx/sites-available/olivia-gpu').write_text('''server {
    listen 443 ssl;
    server_name HOST_ADDRESS;
    ssl_certificate /etc/olivia-gpu/server.crt;
    ssl_certificate_key /etc/olivia-gpu/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 256m;
    access_log off;
    location /v1/ {
        proxy_pass http://127.0.0.1:18880;
        proxy_request_buffering off;
        proxy_read_timeout 60s;
        proxy_set_header Host $host;
    }
    location / { return 404; }
}
'''.replace('HOST_ADDRESS', host))
target=Path('/etc/nginx/sites-enabled/olivia-gpu')
if not target.exists():target.symlink_to('/etc/nginx/sites-available/olivia-gpu')
default=Path('/etc/nginx/sites-enabled/default')
if default.is_symlink():default.unlink()
subprocess.run(['nginx','-t'],check=True)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','--now','olivia-gpu'],check=True)
subprocess.run(['systemctl','reload','nginx'],check=True)
print('Acceptance API installed; capabilities disabled until assets pass verification.')
