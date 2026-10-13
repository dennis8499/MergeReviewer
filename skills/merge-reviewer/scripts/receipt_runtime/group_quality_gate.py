"""Native Group v6 adapter; legacy Group work stays on its original package."""
from pathlib import Path
import unified_workflow as workflow
from workspace_context import group_root as validate_group_root


def load_group_contract(root, work_id):
    return workflow.load_contract(Path(root), work_id, group=True)


def group_snapshot(root, work_id):
    return workflow.snapshot(Path(root), work_id, group=True)


def validate_record_group(root, work_id):
    return workflow.validate_record(Path(root), work_id, group=True)


def check_group(root, work_id, gate):
    return workflow.check(Path(root), work_id, gate, group=True)
