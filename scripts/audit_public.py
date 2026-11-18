"""Scan tracked files only; print filenames/rule names, never matching secrets."""
import re
import subprocess
from pathlib import Path

def scan(root):
    root=Path(root)
    result=subprocess.run(['git','-C',str(root),'ls-files','-z'],capture_output=True,check=True)
    files=[Path(p.decode('utf-8')) for p in result.stdout.split(b'\0') if p]
    rules={
        'credential':re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)'),
        'user_home':re.compile(
            r'(?i)(?:[A-Z]:[\\/]Users[\\/][^\\/\s<>]+|'
            + '/' + r'Users/[^/\s<>]+|' + '/' + r'home/[^/\s<>]+)'
        ),
    }
    failures=[]
    uuid_re=re.compile(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b',re.I)
    for rel in files:
        if rel.name in {'bridge.json','config.toml','.env'} or rel.suffix in {'.token','.sqlite3','.db','.log','.zip','.pyc'}:
            failures.append((str(rel),'private/generated file'))
        text=(root/rel).read_text('utf-8')
        for name,pattern in rules.items():
            if pattern.search(text):failures.append((str(rel),name))
        if any(not value.startswith('00000000-0000-4000-8000-') for value in uuid_re.findall(text)):
            failures.append((str(rel),'non-synthetic UUID'))
    return files,failures

if __name__=='__main__':
    files,failures=scan(Path(__file__).resolve().parents[1])
    for path,rule in failures:print(f'{path}: {rule}')
    if failures:raise SystemExit(1)
    print(f'Public-tree scan passed: {len(files)} tracked text files.')
