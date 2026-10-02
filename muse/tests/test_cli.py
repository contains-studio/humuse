import json
import stat

from openpin_muse.cli import main


def test_token_prompt_does_not_print_secret(tmp_path, monkeypatch, capsys):
    token = "mgst_" + "A" * 43
    monkeypatch.delenv("MUSEGADGET_SDK_TOKEN", raising=False)
    monkeypatch.setattr("getpass.getpass", lambda _: token)
    assert main(["--state-dir", str(tmp_path), "token"]) == 0
    path = tmp_path / "muse/sdk_token"
    assert path.read_text().strip() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert token not in capsys.readouterr().out


def test_link_writes_private_qr_without_printing_pairing_secret(tmp_path, capsys):
    qr = tmp_path / "link.png"
    assert main(["--state-dir", str(tmp_path / "state"), "link",
                 "--public-url", "https://pin.example.com", "--qr", str(qr)]) == 0
    assert qr.read_bytes().startswith(b"\x89PNG")
    assert stat.S_IMODE(qr.stat().st_mode) == 0o600
    state = json.loads((tmp_path / "state/pairing.json").read_text())
    assert state["pairing"]["public_url"] == "https://pin.example.com"
    assert "/api/dev/pair/" not in capsys.readouterr().out


def test_serve_refuses_missing_muse_pairing(tmp_path, capsys):
    assert main(["--state-dir", str(tmp_path), "serve", "--public-url", "https://pin.example.com"]) == 1
    assert "pair-muse" in capsys.readouterr().err
