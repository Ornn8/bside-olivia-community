"""Verified HTTPS using system trust plus bundled public CA roots."""
from pathlib import Path
import ssl


def client_tls_context():
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(Path(__file__).with_name('gpu_ca_bundle.txt')))
    return context
