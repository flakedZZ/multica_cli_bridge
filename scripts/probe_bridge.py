"""Read-only health probe; no task creation, messages, or file uploads."""
import argparse
import json
import sys
import tomllib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from plugins.local_multica.client import request

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-config',type=Path,required=True)
    args=parser.parse_args()
    config=tomllib.loads(args.plugin_config.read_text('utf-8'))['bridge']
    if not config['allowed_users']:raise ValueError('No authorized probe user configured')
    result=request(config['url'],config['token_file'],dict(operation='health',platform='qq',
                   user_id=config['allowed_users'][0],session_id='local-health-check'))
    assert result.get('cli_ready') is True
    print(json.dumps({'bridge_ready':True,'persistent_cli_project_access':True}))

if __name__=='__main__':main()
