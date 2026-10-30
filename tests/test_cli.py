import json

from depfix.cli import main
from tests.helpers import app_repo


def test_end_to_end_scan_propose_verify_report_trace(tmp_path, capsys):
    app_repo(tmp_path)
    assert main(["scan", str(tmp_path), "--json"]) == 0
    fid = json.loads(capsys.readouterr().out)[0]["id"]

    assert main(["propose", fid, "--repo", str(tmp_path)]) == 0
    assert "logging.warning" in capsys.readouterr().out

    assert main(["verify", fid, "--repo", str(tmp_path)]) == 2  # refuses without the flag
    assert "WITHOUT network" in capsys.readouterr().err

    assert main(["verify", fid, "--repo", str(tmp_path), "--allow-local-exec"]) == 0
    assert "VERIFIED" in capsys.readouterr().out

    assert main(["report", "--repo", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["by_status"] == {"verified": 1}

    assert main(["trace", fid, "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    for stage in ("1. SCAN", "2. RULE", "3. NAME RESOLUTION", "4. PROPOSE", "5. VERIFY"):
        assert stage in out
    # nothing is applied in drop 1: the real source file is untouched
    assert (tmp_path / "src/app.py").read_text().count("logging.warn(") == 1


def test_cannot_verify_before_proposing(tmp_path, capsys):
    app_repo(tmp_path)
    main(["scan", str(tmp_path), "--json"])
    fid = json.loads(capsys.readouterr().out)[0]["id"]
    assert main(["verify", fid, "--repo", str(tmp_path), "--allow-local-exec"]) == 2
    assert "propose a fix first" in capsys.readouterr().err


def test_unknown_finding_is_a_clean_error(tmp_path, capsys):
    app_repo(tmp_path)
    assert main(["propose", "nope", "--repo", str(tmp_path)]) == 2
    assert "no finding" in capsys.readouterr().err
