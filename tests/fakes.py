"""Fakes for Google services. Tests never touch the network."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.sheets import SheetStore, column_letter

FIXTURES = Path(__file__).parent / "fixtures"
_SERIAL_EPOCH = datetime(1899, 12, 30)
_RANGE = re.compile(
    r"^'(?P<tab>(?:[^']|'')+)'(?:!(?P<c1>[A-Z]*)(?P<r1>\d*)(?::(?P<c2>[A-Z]*)(?P<r2>\d*))?)?$"
)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?$")


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _serial(value: date | datetime) -> float:
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    return (value.replace(tzinfo=None) - _SERIAL_EPOCH).total_seconds() / 86400


class FakeSheetsBackend:
    """In-memory spreadsheet that mimics USER_ENTERED parsing and API read behaviour.

    Cells hold what was entered. Reads apply Sheets' rules: `'text` is text, ISO dates become
    serial numbers, bare numeric text becomes a number, formulas read as `formula_value` (or the
    formula itself when `formulas=True`). Trailing empty cells and rows are dropped like the API.
    """

    def __init__(self, tabs: dict[str, list[list[Any]]], formula_value: Any = None):
        self.tabs = {name: [list(row) for row in rows] for name, rows in tabs.items()}
        self.formula_value = formula_value
        self.calls: list[tuple[str, Any]] = []

    @classmethod
    def from_fixtures(cls, directory: Path = FIXTURES) -> FakeSheetsBackend:
        files = {
            "Master": "master.json",
            "Staff": "staff.json",
            "Catalog": "catalog.json",
            "LetterCodes": "lettercodes.json",
            "Config": "config.json",
        }
        tabs: dict[str, list[list[Any]]] = {}
        for tab, filename in files.items():
            data = json.loads((directory / filename).read_text(encoding="utf-8"))
            columns = data["columns"]
            tabs[tab] = [columns] + [
                [_stored(row.get(column)) for column in columns] for row in data["rows"]
            ]
        tabs["AuditLog"] = [
            ["Timestamp", "User", "Task ID", "Action", "Field", "Old value", "New value", "Reason"]
        ]
        tabs["_state"] = [["ID", "Status", "Owner", "Doer", "Notified doer", "Notified at"]]
        tabs["_failures"] = [["Key", "Count", "Last error", "Last at"]]
        return cls(tabs)

    # Helpers for tests ---------------------------------------------------------------------

    def column(self, tab: str, header: str) -> list[Any]:
        rows = self.tabs[tab]
        index = rows[0].index(header)
        return [self._read(row[index] if index < len(row) else None) for row in rows[1:]]

    def raw(self, tab: str, row_number: int, header: str) -> Any:
        rows = self.tabs[tab]
        return rows[row_number - 1][rows[0].index(header)]

    # SheetsBackend -------------------------------------------------------------------------

    def sheet_titles(self) -> list[str]:
        self.calls.append(("titles", None))
        return list(self.tabs)

    def get(self, ranges: list[str], formulas: bool = False) -> list[list[list[Any]]]:
        self.calls.append(("get", tuple(ranges)))
        return [self._get(range_, formulas) for range_ in ranges]

    def update(self, data: list[tuple[str, list[list[Any]]]]) -> None:
        self.calls.append(("update", [range_ for range_, _ in data]))
        for range_, values in data:
            tab, first_row, first_col, _, _ = self._parse(range_)
            for r, row in enumerate(values):
                for c, value in enumerate(row):
                    if value is not None:
                        self._set(tab, first_row + r, first_col + c, value)

    def append(self, range_: str, rows: list[list[Any]]) -> None:
        self.calls.append(("append", range_))
        tab = self._parse(range_)[0]
        grid = self.tabs[tab]
        while grid and all(cell in (None, "") for cell in grid[-1]):
            grid.pop()
        for row in rows:
            grid.append([value if value is not None else "" for value in row])

    def clear(self, range_: str) -> None:
        self.calls.append(("clear", range_))
        tab, first_row, first_col, last_row, last_col = self._parse(range_)
        grid = self.tabs[tab]
        for r in range(first_row, min(last_row, len(grid) - 1) + 1):
            for c in range(first_col, min(last_col, len(grid[r]) - 1) + 1):
                grid[r][c] = ""

    # Internals -----------------------------------------------------------------------------

    def _parse(self, range_: str) -> tuple[str, int, int, int, int]:
        """-> (tab, first row index, first col index, last row index, last col index), 0-based."""
        match = _RANGE.match(range_)
        if not match:
            raise ValueError(f"unsupported range {range_}")
        tab = match["tab"].replace("''", "'")
        if tab not in self.tabs:
            raise ValueError(f"no tab {tab}")
        first_row = int(match["r1"]) - 1 if match["r1"] else 0
        first_col = _column_index(match["c1"]) if match["c1"] else 0
        if match["c2"] is None and match["r2"] is None:
            last_row = first_row if match["r1"] else 10**6
            last_col = first_col if match["c1"] else 10**6
        else:
            last_row = int(match["r2"]) - 1 if match["r2"] else 10**6
            last_col = _column_index(match["c2"]) if match["c2"] else 10**6
        return tab, first_row, first_col, last_row, last_col

    def _get(self, range_: str, formulas: bool) -> list[list[Any]]:
        tab, first_row, first_col, last_row, last_col = self._parse(range_)
        out = []
        for row in self.tabs[tab][first_row : last_row + 1]:
            values = [self._read(cell, formulas) for cell in row[first_col : last_col + 1]]
            while values and values[-1] in (None, ""):
                values.pop()
            out.append(values)
        while out and not out[-1]:
            out.pop()
        return out

    def _set(self, tab: str, row: int, col: int, value: Any) -> None:
        grid = self.tabs[tab]
        while len(grid) <= row:
            grid.append([])
        while len(grid[row]) <= col:
            grid[row].append("")
        grid[row][col] = value

    def _read(self, cell: Any, formulas: bool = False) -> Any:
        if not isinstance(cell, str):
            return cell
        if cell.startswith("'"):
            return cell[1:]
        if cell.startswith("="):
            return cell if formulas else self.formula_value
        if _ISO_DATE.match(cell):
            return _serial(date.fromisoformat(cell))
        if _ISO_DATETIME.match(cell):
            return _serial(datetime.fromisoformat(cell))
        if _NUMBER.match(cell):
            return float(cell) if "." in cell else int(cell)
        return cell


def _stored(value: Any) -> Any:
    """Fixture JSON value -> what the sheet holds (dates as serials, like typed-in dates)."""
    if isinstance(value, str) and _ISO_DATE.match(value):
        return _serial(date.fromisoformat(value))
    return value


class FakeSheetStore(SheetStore):
    """The real SheetStore over an in-memory sheet loaded from tests/fixtures/*.json."""

    def __init__(self, backend: FakeSheetsBackend | None = None):
        self.backend = backend or FakeSheetsBackend.from_fixtures()
        super().__init__(self.backend)


__all__ = ["FakeSheetStore", "FakeSheetsBackend", "column_letter"]
