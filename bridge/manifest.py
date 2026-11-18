"""Machine-readable image list in the executing agent's final comment."""
import json
import re


MARKER = "[multica-delivery:v1]"


def inspect_delivery(service, root, source, images, files, error):
    required = any(MARKER in str(issue.get("description") or "") for issue in (root, source))
    available = images + files
    if available and not required:
        return {"delivery_complete": True, "manifest_required": False, "images": images, "files": files}
    roots = service.cli.run(["issue", "comment", "list", source["id"], "--roots-only", "--summary", "--compact"])
    if not isinstance(roots, list):
        raise error("CLI交付评论格式不兼容", 502)
    authors = {source.get("assignee_id"), service.settings.mika_id}
    comments = [c for c in roots if isinstance(c, dict) and c.get("author_type") == "agent" and c.get("author_id") in authors]
    if not required:
        return {"delivery_complete": any(str(c.get("content") or "").strip() for c in comments),
                "manifest_required": False, "images": images, "files": files}
    waiting = {"delivery_complete": False, "manifest_required": True, "images": [], "files": [], "missing": ["交付附件清单"]}
    if not comments:
        return waiting
    latest = max(comments, key=lambda c: c.get("created_at") or "")
    thread = service.cli.run(["issue", "comment", "list", source["id"], "--thread", latest["id"], "--tail", "10", "--compact"])
    if not isinstance(thread, list):
        raise error("CLI交付清单格式不兼容", 502)
    comments = [c for c in thread if isinstance(c, dict) and c.get("author_type") == "agent" and c.get("author_id") in authors]
    if not comments:
        return waiting
    latest = max(comments, key=lambda c: c.get("created_at") or "")
    blocks = re.findall(r"```json\s*\n(.*?)\n```", str(latest.get("content") or ""), re.S)
    manifests = []
    for block in blocks:
        if "multica_delivery" not in block:
            continue
        try:
            manifests.append(json.loads(block))
        except ValueError:
            return waiting
    if len(manifests) != 1 or not isinstance(manifests[0], dict) or type(manifests[0].get("multica_delivery")) is not int or manifests[0]["multica_delivery"] != 1:
        return waiting
    names = manifests[0].get("attachments")
    if (not isinstance(names, list) or len(names) > 16 or
            any(not isinstance(n, str) or not n or len(n) > 180 or
                any(c in n for c in '\\/:*?"<>|\x00') or n in {".", ".."} for n in names) or
            len(set(names)) != len(names)):
        return waiting
    selected = []
    missing = []
    for name in names:
        matches = [i for i in available if i["filename"] == name]
        if len(matches) != 1:
            missing.append(name)
        else:
            selected.append(matches[0])
    return {"delivery_complete": not missing, "manifest_required": True,
            "result_text": str(latest.get("content") or ""), "result_comment_id": latest.get("id"),
            "expected_attachments": names, "missing": missing,
            "images": [i for i in selected if i in images], "files": [i for i in selected if i in files]}
