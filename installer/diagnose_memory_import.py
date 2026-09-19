"""Standalone offline import probe. Does not read profiles, keys, or user memory."""
import importlib
import json
import os
from pathlib import Path
import sys


def main():
    os.environ['MEM0_TELEMETRY'] = 'False'
    checks = []
    # Probe the actual top-level import first, before individual dependencies
    # can change DLL load order and hide the original failure.
    for name in ('mem0', 'qdrant_client', 'sqlite3', 'numpy', 'grpc',
                 'google.protobuf', 'pywintypes', 'win32file'):
        try:
            importlib.import_module(name)
            checks.append({'module': name, 'status': 'ok'})
        except Exception as error:
            trace = error.__traceback__
            frames = []
            while trace is not None:
                frames.append(trace.tb_frame.f_globals.get('__name__', 'unknown'))
                trace = trace.tb_next
            checks.append({'module': name, 'status': 'failed',
                           'exception': type(error).__name__, 'detail': str(error),
                           'winerror': getattr(error, 'winerror', None),
                           'modules': frames})
    report = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('memory-import-report.json')
    report.write_text(json.dumps({'python': sys.version.split()[0], 'checks': checks},
                                ensure_ascii=False, indent=2), encoding='utf-8')
    print('Report saved:', report.name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
