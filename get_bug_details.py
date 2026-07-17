import argparse
import copy
import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx


def load_config(config_path: str = "config.json") -> Dict[str, Any]:
    """Load config from config.json and overlay environment variables."""
    config_file = Path(config_path)
    data: Dict[str, Any] = {}

    if config_file.exists():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {config_path}: {exc}") from exc

    env_overrides = {
        "mailshell_username": os.getenv("MAILSHELL_USERNAME"),
        "mailshell_password": os.getenv("MAILSHELL_PASSWORD"),
        "bugzilla_token": os.getenv("BUGZILLA_TOKEN"),
        "bz_rest_url": os.getenv("BZ_REST_URL"),
        "default_bug_id": os.getenv("BUG_ID"),
    }

    for key, value in env_overrides.items():
        if value not in (None, ""):
            data[key] = value

    return data


class BugzillaFetcher:
    """Fetch Bugzilla bug details and prepare sheet-friendly rows."""

    def __init__(self, settings: Dict[str, Any]):
        self.auth_user = settings.get("mailshell_username", "")
        self.auth_pass = settings.get("mailshell_password", "")
        self.bugzilla_token = settings.get("bugzilla_token", "")
        self.bz_rest_url = settings.get(
            "bz_rest_url",
            "https://dev.mailshell.net/corp/bugzilla/rest/bug",
        ).rstrip("/")
        self._bug_cache: Dict[str, Dict[str, Any]] = {}
        self._node_cache: Dict[str, Dict[str, Any]] = {}

    def _request(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request_params = dict(params or {})
        headers = {"Accept": "application/json"}
        if self.bugzilla_token:
            request_params["api_key"] = self.bugzilla_token

        response = httpx.get(
            url,
            params=request_params,
            headers=headers,
            auth=(self.auth_user, self.auth_pass) if self.auth_user else None,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def _fetch_bug_record(self, bug_id: str) -> Dict[str, Any]:
        """Fetch and cache the raw bug record for a single bug id."""
        bug_id = str(bug_id)
        if bug_id in self._bug_cache:
            return self._bug_cache[bug_id]

        bug_url = f"{self.bz_rest_url}/{bug_id}"
        payload = self._request(bug_url)
        bugs = payload.get("bugs", [])
        if not bugs:
            raise ValueError(f"No bug found for bug id {bug_id}")

        bug = bugs[0]
        self._bug_cache[bug_id] = bug
        return bug

    def _fetch_comments(self, bug_id: str) -> List[Dict[str, Any]]:
        comments_payload = self._request(f"{self.bz_rest_url}/{bug_id}/comment")
        comments_root = comments_payload.get("bugs", {})
        if isinstance(comments_root, dict):
            return comments_root.get(str(bug_id), {}).get("comments", [])
        return []

    def _fetch_attachments(self, bug_id: str) -> List[Dict[str, Any]]:
        attachments_payload = self._request(f"{self.bz_rest_url}/{bug_id}/attachment")
        attachments_root = attachments_payload.get("bugs", {})
        if isinstance(attachments_root, dict):
            return attachments_root.get(str(bug_id), [])
        return []

    def _fetch_history(self, bug_id: str) -> List[Dict[str, Any]]:
        history_payload = self._request(f"{self.bz_rest_url}/{bug_id}/history")
        history_root = history_payload.get("bugs", history_payload.get("history", []))

        if isinstance(history_root, list):
            return history_root
        if isinstance(history_root, dict):
            return history_root.get(str(bug_id), {}).get("history", [])
        return []

    def _get_comments(self, bug_id: str) -> List[Dict[str, Any]]:
        comments = self._fetch_comments(bug_id)
        if not comments:
            return []

        def comment_sort_key(comment: Dict[str, Any]) -> tuple:
            return (
                comment.get("count", -1),
                comment.get("creation_time", ""),
                comment.get("time", ""),
                comment.get("id", -1),
            )

        return sorted(comments, key=comment_sort_key)

    def _clean_csv_text(self, value: Any) -> str:
        """Convert multiline markdown-ish text into a single CSV-friendly line."""
        if value is None:
            return ""
        text = str(value).replace("\r\n", "\n").replace("\r", "\n")
        text = " ".join(part.strip() for part in text.split("\n") if part.strip())
        return " ".join(text.split())

    def _select_bug_fields(self, bug: Dict[str, Any]) -> Dict[str, Any]:
        """Keep the bug payload focused on the fields you care about."""
        return {
            "id": bug.get("id"),
            "summary": bug.get("summary", ""),
            "status": bug.get("status", ""),
            "priority": bug.get("priority", ""),
            "severity": bug.get("severity", ""),
            "resolution": bug.get("resolution", ""),
            "last_change_time": bug.get("last_change_time", ""),
            "is_confirmed": bug.get("is_confirmed", False),
            "groups": bug.get("groups", []),
            "whiteboard": bug.get("whiteboard", ""),
            "creator_detail": bug.get("creator_detail", {}),
            "cc": bug.get("cc", []),
            "cc_detail": bug.get("cc_detail", []),
            "target_milestone": bug.get("target_milestone", ""),
            "qa_contact": bug.get("qa_contact", ""),
            "actual_time": bug.get("actual_time", 0),
            "op_sys": bug.get("op_sys", ""),
            "is_creator_accessible": bug.get("is_creator_accessible", False),
            "assigned_to": bug.get("assigned_to", ""),
            "assigned_to_detail": bug.get("assigned_to_detail", {}),
            "dupe_of": bug.get("dupe_of"),
            "deadline": bug.get("deadline", ""),
            "estimated_time": bug.get("estimated_time", 0),
            "product": bug.get("product", ""),
            "creation_time": bug.get("creation_time", ""),
            "component": bug.get("component", ""),
            "flags": bug.get("flags", []),
            "url": bug.get("url", ""),
            "platform": bug.get("platform", ""),
            "see_also": bug.get("see_also", []),
            "is_open": bug.get("is_open", False),
            "creator": bug.get("creator", ""),
            "version": bug.get("version", ""),
            "depends_on": [str(dep_id) for dep_id in (bug.get("depends_on") or [])],
            "blocks": [str(block_id) for block_id in (bug.get("blocks") or [])],
            "keywords": bug.get("keywords", []),
            "alias": bug.get("alias", []),
            "classification": bug.get("classification", ""),
            "update_token": bug.get("update_token", ""),
            "remaining_time": bug.get("remaining_time", 0),
        }

    def _build_bug_node(
        self,
        bug_id: str,
        visited: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """
        Build a recursive bug node for a bug and all of its dependencies.

        Each node includes the selected bug fields, all comments, and
        nested dependency bugs in the same shape.
        """
        bug_id = str(bug_id)
        visited = set(visited or set())

        if bug_id in self._node_cache:
            return copy.deepcopy(self._node_cache[bug_id])

        if bug_id in visited:
            return {
                "bug_id": bug_id,
                "cycle_detected": True,
                "depends_on": [],
                "depends_on_bugs": [],
            }

        visited.add(bug_id)
        bug = self._fetch_bug_record(bug_id)
        dependency_ids = [str(dep_id) for dep_id in (bug.get("depends_on") or [])]
        comments = self._get_comments(bug_id)
        depends_on_bugs = [
            self._build_bug_node(dep_id, visited.copy())
            for dep_id in dependency_ids
        ]

        node = {
            "bug_id": bug.get("id", bug_id),
            "bug": self._select_bug_fields(bug),
            "comments": comments,
            "depends_on_ids": dependency_ids,
            "depends_on_bugs": depends_on_bugs,
        }
        self._node_cache[bug_id] = copy.deepcopy(node)
        return node

    def fetch_bug_details(self, bug_id: str) -> Dict[str, Any]:
        """
        Fetch the core bug fields plus all comments and recursive dependency data.
        """
        bug = self._fetch_bug_record(bug_id)
        comments = self._get_comments(bug_id)
        dependency_tree = self._build_bug_node(bug_id)

        return {
            "bug": bug,
            "comments": comments,
            "dependency_tree": dependency_tree,
        }

    def build_output_document(self, bug_id: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """Build a structured JSON document for file export."""
        bug = details["bug"]
        return {
            "metadata": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "bug_id_requested": str(bug_id),
                "source": "bugzilla",
                "api_base_url": self.bz_rest_url,
            },
            "bug": self._select_bug_fields(bug),
            "comments": details["comments"],
            "depends_on_bugs": details["dependency_tree"].get("depends_on_bugs", []),
        }

    def _flatten_bug_rows(
        self,
        node: Dict[str, Any],
        root_bug_id: str,
        parent_bug_id: str = "",
        depth: int = 0,
        path: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Flatten the dependency tree into CSV rows."""
        path = list(path or [])

        if node.get("cycle_detected"):
            bug_id = str(node.get("bug_id", ""))
            return [{
                "root_bug_id": str(root_bug_id),
                "bug_id": bug_id,
                "parent_bug_id": parent_bug_id,
                "depth": depth,
                "path": " > ".join(path + [bug_id]),
                "cycle_detected": True,
            }]

        bug = node.get("bug", {})
        comments = node.get("comments", [])
        current_bug_id = str(node.get("bug_id", bug.get("id", "")))
        current_path = path + [current_bug_id]

        row = {
            "root_bug_id": str(root_bug_id),
            "bug_id": current_bug_id,
            "parent_bug_id": parent_bug_id,
            "depth": depth,
            "path": " > ".join(current_path),
            "summary": self._clean_csv_text(bug.get("summary", "")),
            "status": bug.get("status", ""),
            "priority": bug.get("priority", ""),
            "severity": bug.get("severity", ""),
            "resolution": bug.get("resolution", ""),
            "product": bug.get("product", ""),
            "component": bug.get("component", ""),
            "version": bug.get("version", ""),
            "platform": bug.get("platform", ""),
            "op_sys": bug.get("op_sys", ""),
            "assigned_to": bug.get("assigned_to", ""),
            "creator": bug.get("creator", ""),
            "creation_time": bug.get("creation_time", ""),
            "last_change_time": bug.get("last_change_time", ""),
            "is_confirmed": bug.get("is_confirmed", False),
            "deadline": bug.get("deadline", ""),
            "whiteboard": self._clean_csv_text(bug.get("whiteboard", "")),
            "depends_on_ids": ", ".join(node.get("depends_on_ids", [])),
            "comment_count": len(comments),
            "comments_text": " || ".join(
                f"[{comment.get('creator', '')} @ "
                f"{comment.get('creation_time') or comment.get('time', '')}] "
                f"{self._clean_csv_text(comment.get('text', ''))}"
                for comment in comments
            ),
        }

        rows = [row]
        for child in node.get("depends_on_bugs", []):
            rows.extend(
                self._flatten_bug_rows(
                    child,
                    root_bug_id=root_bug_id,
                    parent_bug_id=current_bug_id,
                    depth=depth + 1,
                    path=current_path,
                )
            )
        return rows

    def write_csv(self, output_path: Path, details: Dict[str, Any], root_bug_id: str) -> None:
        """Write a clean flattened CSV from the bug tree."""
        rows = self._flatten_bug_rows(details["dependency_tree"], root_bug_id=root_bug_id)
        if not rows:
            output_path.write_text("", encoding="utf-8")
            return

        fieldnames: List[str] = []
        for row in rows:
            for key in row.keys():
                if key not in fieldnames:
                    fieldnames.append(key)

        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch full Bugzilla bug details.")
    parser.add_argument("bug_id", nargs="?", help="Bug number to fetch")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config.json",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the result as JSON",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Write the structured JSON to this file. Defaults to bug_<id>_details.json.",
    )
    parser.add_argument(
        "--csv-output",
        default="",
        help="Write a flattened CSV to this file. Defaults to bug_<id>_details.csv when provided.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config = load_config(args.config)
    bug_id = args.bug_id or config.get("default_bug_id")
    if not bug_id:
        raise SystemExit("Provide a bug id either as an argument or via BUG_ID/default_bug_id.")

    fetcher = BugzillaFetcher(config)
    details = fetcher.fetch_bug_details(str(bug_id))
    document = fetcher.build_output_document(str(bug_id), details)

    output_path = Path(args.output or f"bug_{bug_id}_details.json")
    output_path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.csv_output:
        csv_path = Path(args.csv_output or f"bug_{bug_id}_details.csv")
        fetcher.write_csv(csv_path, details, root_bug_id=str(bug_id))

    if args.pretty:
        print(json.dumps(document, indent=2, ensure_ascii=False))
    else:
        print(json.dumps({"output_file": str(output_path)}, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# python3 get_bug_details.py --csv-output bug_details.csv