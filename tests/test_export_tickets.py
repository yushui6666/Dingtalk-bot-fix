"""CSV 导出命令行默认路径测试。"""

from __future__ import annotations

from datetime import datetime


def test_main_uses_timestamped_default_output(monkeypatch, tmp_path):
    from scripts import export_tickets

    class FrozenDatetime:
        @classmethod
        def now(cls):
            return datetime(2026, 8, 20, 12, 34, 56)

    captured = []
    monkeypatch.setattr(export_tickets, "BASE_DIR", tmp_path)
    monkeypatch.setattr(export_tickets, "datetime", FrozenDatetime, raising=False)
    monkeypatch.setattr(export_tickets, "export", lambda group, output: captured.append((group, output)))
    monkeypatch.setattr("sys.argv", ["export_tickets.py"])

    export_tickets.main()

    assert captured == [(None, tmp_path / "data" / "tickets_export_20260820_123456.csv")]


def test_main_preserves_explicit_output_path(monkeypatch, tmp_path):
    from scripts import export_tickets

    explicit = tmp_path / "指定.csv"
    captured = []
    monkeypatch.setattr(export_tickets, "export", lambda group, output: captured.append((group, output)))
    monkeypatch.setattr("sys.argv", ["export_tickets.py", "--group", "测试群", "-o", str(explicit)])

    export_tickets.main()

    assert captured == [("测试群", explicit)]
