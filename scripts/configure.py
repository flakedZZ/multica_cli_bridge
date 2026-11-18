"""Generate private configuration; never print generated tokens."""
import argparse
import json
import os
import secrets
from pathlib import Path
from uuid import UUID

def generate(destination, *, cli_path, workspace_id, project_id, agent_id, users,
             planner_id='', profile='', bridge_url='http://host.docker.internal:18765'):
    for value in (workspace_id,project_id,agent_id,planner_id or agent_id):UUID(value)
    if not users or any(not isinstance(u,str) or not u.isdecimal() or int(u)<=0 for u in users):
        raise ValueError('At least one explicit, numeric allowed QQ user is required')
    if not Path(cli_path).is_absolute() or not Path(cli_path).is_file():
        raise ValueError('cli_path must point to an existing absolute executable path')
    destination=Path(destination)
    # Never overwrite a production configuration or regenerate its live token.
    destination.mkdir(parents=True,exist_ok=False)
    os.chmod(destination,0o700)
    token=secrets.token_urlsafe(48)
    config=dict(cli_path=str(cli_path),workspace_id=workspace_id,project_id=project_id,
                assignee_id=agent_id,mika_id=planner_id or agent_id,profile=profile,
                allowed_users=users,token_file='bridge.token',state_dir='state',cli_timeout=8,
                max_submissions_per_hour=5,max_discussions_per_hour=10,allow_direct_submit=False)
    (destination/'bridge.json').write_text(json.dumps(config,indent=2)+'\n',encoding='utf-8')
    plugin=destination/'plugin'
    plugin.mkdir()
    for path in (destination/'bridge.token',plugin/'bridge.token'):
        path.write_text(token,encoding='ascii')
        os.chmod(path,0o600)
    q=lambda value:json.dumps(value,ensure_ascii=True)
    (plugin/'config.toml').write_text(f'''[plugin]
enabled = false
config_version = "1.6.1"

[bridge]
url = {q(bridge_url)}
token_file = "/MaiMBot/data/plugins/local.multica/bridge.token"
allowed_users = {q(users)}
poll_seconds = 20

[routing]
mode = "complex"
auto_return_images = true

[closure]
enabled = true

[delivery]
poll_seconds = 20
max_failures = 5
''',encoding='utf-8')
    return config

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--cli-path',required=True)
    parser.add_argument('--workspace-id',required=True)
    parser.add_argument('--project-id',required=True)
    parser.add_argument('--agent-id',required=True)
    parser.add_argument('--planner-id',default='')
    parser.add_argument('--profile',default='')
    parser.add_argument('--allowed-user',action='append',required=True)
    parser.add_argument('--bridge-url',default='http://host.docker.internal:18765')
    args=parser.parse_args()
    generate(args.output_dir,cli_path=args.cli_path,workspace_id=args.workspace_id,project_id=args.project_id,
             agent_id=args.agent_id,planner_id=args.planner_id,users=args.allowed_user,
             profile=args.profile,bridge_url=args.bridge_url)
    print('Private configuration generated; plugin remains disabled. No credentials printed.')

if __name__=='__main__':main()
