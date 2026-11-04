import json
import sys

from workflow_ledger import JsonlStore
from examples.quickstart import main


def test_public_api_quickstart_records_workflow_and_reports_health(tmp_path, capsys, monkeypatch):
    path = tmp_path / "observations.jsonl"
    monkeypatch.setattr(sys, "argv", ["quickstart", "--store", str(path)])

    assert main() == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["records_written"] == 3
    assert summary["recorder_diagnostics"] == {
        "rejected_records": 0,
        "delivery_errors": 0,
        "filtered_records": 0,
    }
    assert summary["store_diagnostics"] == {"malformed_records": 0, "storage_errors": 0}
    assert [record.kind for record in JsonlStore(path).read()] == ["workflow", "agent", "tool"]
