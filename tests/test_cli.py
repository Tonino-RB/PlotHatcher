from pathlib import Path

import click
import pytest
import vpype_cli
from click.testing import CliRunner
from lxml import etree

from fonthatch.cli.standalone import main as standalone_main

FIXTURES = Path(__file__).parent / "fixtures"


def _layers(svg_text: str) -> dict[str, etree._Element]:
    root = etree.fromstring(svg_text.encode("utf-8"))
    out = {}
    for el in root.iter():
        label = el.get("{http://www.inkscape.org/namespaces/inkscape}label")
        if label:
            out[label] = el
    return out


# --- standalone `fonthatch` CLI --------------------------------------------


def test_standalone_cli_writes_hidden_text_and_visible_hatched_layers(tmp_path):
    out_path = tmp_path / "out.svg"
    result = CliRunner().invoke(standalone_main, [str(FIXTURES / "mixed.svg"), str(out_path)])
    assert result.exit_code == 0, result.output
    assert out_path.exists()
    layers = _layers(out_path.read_text())
    assert "text" in layers and "hatched" in layers
    assert "display:none" in layers["text"].get("style", "")
    assert "display:none" not in layers["hatched"].get("style", "")


def test_standalone_cli_refuses_to_overwrite_input(tmp_path):
    src = tmp_path / "mine.svg"
    src.write_text((FIXTURES / "mixed.svg").read_text())
    original_contents = src.read_text()

    result = CliRunner().invoke(standalone_main, [str(src), str(src)])
    assert result.exit_code != 0
    # must not have touched the file before raising
    assert src.read_text() == original_contents


def test_standalone_cli_rejects_invalid_length_option(tmp_path):
    out_path = tmp_path / "out.svg"
    result = CliRunner().invoke(
        standalone_main, [str(FIXTURES / "mixed.svg"), str(out_path), "--pen-width", "not-a-length"]
    )
    assert result.exit_code == 2
    assert not out_path.exists()


def test_standalone_cli_zigzag_fill_type(tmp_path):
    out_path = tmp_path / "out.svg"
    result = CliRunner().invoke(
        standalone_main,
        [
            str(FIXTURES / "mixed.svg"),
            str(out_path),
            "--fill-type",
            "zigzag",
            "--zigzag-passes",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    layers = _layers(out_path.read_text())
    assert list(layers["hatched"])


def test_standalone_cli_singleline_mode(tmp_path):
    out_path = tmp_path / "out.svg"
    result = CliRunner().invoke(
        standalone_main,
        [str(FIXTURES / "mixed.svg"), str(out_path), "--mode", "singleline", "--font", "timesr*"],
    )
    assert result.exit_code == 0, result.output
    layers = _layers(out_path.read_text())
    assert list(layers["hatched"])


def test_standalone_cli_rejects_out_of_range_zigzag_passes(tmp_path):
    out_path = tmp_path / "out.svg"
    result = CliRunner().invoke(
        standalone_main,
        [str(FIXTURES / "mixed.svg"), str(out_path), "--fill-type", "zigzag", "--zigzag-passes", "5"],
    )
    assert result.exit_code == 2
    assert not out_path.exists()


def test_standalone_cli_missing_input_file_reports_usage_error():
    result = CliRunner().invoke(standalone_main, [str(FIXTURES / "does_not_exist.svg"), "/tmp/never-written.svg"])
    assert result.exit_code == 2


# --- `vpype` plugin commands (fonthatch / fonthatch-write) ------------------


def test_vpype_plugin_pipeline_writes_hidden_text_and_hatched_layers(tmp_path):
    out_path = tmp_path / "out.svg"
    vpype_cli.execute(f'read "{FIXTURES / "mixed.svg"}" fonthatch --pen-width 0.35mm fonthatch-write "{out_path}"')
    assert out_path.exists()
    layers = _layers(out_path.read_text())
    assert "text" in layers and "hatched" in layers
    assert "display:none" in layers["text"].get("style", "")
    assert "display:none" not in layers["hatched"].get("style", "")


def test_vpype_plugin_requires_a_document_read_from_a_file():
    """fonthatch needs to re-read the source SVG for <text> (vpype's own
    reader drops it) — a pipeline that never `read` a file at all has no
    METADATA_FIELD_SOURCE to recover it from, and must fail with a clear
    usage error rather than crashing deeper in the pipeline."""
    with pytest.raises(click.UsageError):
        vpype_cli.execute("line 0 0 10in 10in fonthatch")


def test_vpype_plugin_write_refuses_to_overwrite_input(tmp_path):
    src = tmp_path / "mine.svg"
    src.write_text((FIXTURES / "mixed.svg").read_text())
    original_contents = src.read_text()

    with pytest.raises(click.UsageError):
        vpype_cli.execute(f'read "{src}" fonthatch fonthatch-write "{src}"')
    assert src.read_text() == original_contents
