# SPDX-License-Identifier: GPL-3.0-only
"""Apply the minimal MaiBot 1.4.1 identity fix, with a backup and strict anchors.

The matching upstream code is GPL-3.0; see licenses/GPL-3.0.txt and NOTICE.md.
This script defaults to a read-only compatibility check.
"""
import argparse
import ast
import hashlib
from pathlib import Path

ANCHOR = '''        if legacy_action:
            # 旧插件通过 action_data 接收业务参数，通过 chat_id 定位聊天流。'''
INSERT = '''        if invocation.tool_name == "multica_mika":
            # local.multica: reserved identity always comes from the host,
            # including empty values. Model arguments cannot supply a fallback.
            trusted = {key: str(context_payload.get(key) or "")
                       for key in ("stream_id", "user_id", "platform")}
            payload.update(trusted)
            payload["_multica_context"] = trusted
'''

def patch_source(source):
    if INSERT in source:
        if source.count(INSERT) != 1:
            raise ValueError('Duplicate identity guards; review the host source')
        return source
    if '_multica_context' in source or source.count(ANCHOR) != 1:
        raise ValueError('Unknown or already modified host version; review instead of overwriting')
    updated = source.replace(ANCHOR, INSERT + ANCHOR)
    ast.parse(updated)
    return updated

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path,help='MaiBot src/plugin_runtime/component_query.py')
    parser.add_argument('--apply',action='store_true',help='write patch after creating a non-overwriting backup')
    args=parser.parse_args()
    original=args.source.read_bytes()
    source=original.decode('utf-8')
    updated=patch_source(source)
    if source==updated:
        print('Identity guard already installed.')
        return
    print('Compatible source SHA256:',hashlib.sha256(original).hexdigest())
    if not args.apply:
        print('Check only. Review host-patch/component_query.patch; use --apply to install.')
        return
    backup=args.source.with_name(args.source.name+'.before-multica-bridge')
    with backup.open('xb') as f:f.write(original)
    if args.source.read_bytes()!=original:
        raise RuntimeError('Host changed concurrently; backup preserved, no overwrite performed')
    args.source.write_text(updated,encoding='utf-8',newline='\n')
    print('Patched. Restart the MaiBot host before enabling the plugin. Backup:',backup.name)

if __name__=='__main__':main()
