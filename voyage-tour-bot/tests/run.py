"""Одна команда приёмки, подробный фактический отчёт и ненулевой код при провале."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import unittest


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = []

    def addSuccess(self, test):
        super().addSuccess(test)
        evidence = getattr(test, "evidence", [])
        self.rows.extend({**e, "test": test.id(), "status": "passed"} for e in evidence)
        if not evidence:
            self.rows.append(
                {
                    "scenario": test.id(),
                    "expected": test.shortDescription() or "Все утверждения выполнены",
                    "actual": "Все проверки выполнены",
                    "status": "passed",
                }
            )

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.rows.append(
            {
                "scenario": test.id(),
                "expected": "Все утверждения выполнены",
                "actual": str(err[1]),
                "status": "failed",
            }
        )

    def addError(self, test, err):
        super().addError(test, err)
        self.rows.append(
            {
                "scenario": test.id(),
                "expected": "Без технических ошибок",
                "actual": type(err[1]).__name__,
                "status": "error",
            }
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stock", type=int)
    parser.add_argument("--buyers", type=int)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--reservation-seconds", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.reservation_seconds <= 86400:
        parser.error("--reservation-seconds должен быть от 1 до 86400")
    os.environ["STOCK_TEST_RESERVATION_SECONDS"] = str(args.reservation_seconds)
    if (
        bool(args.stock is not None) != bool(args.buyers is not None)
        or args.repeats < 5
    ):
        parser.error("--stock и --buyers задаются вместе; --repeats не меньше 5")
    if args.stock is not None:
        if not 1 <= args.stock <= 1000000 or not 1 <= args.buyers <= 500:
            parser.error("stock 1..1000000, buyers 1..500")
        os.environ["STOCK_TEST_STOCK"] = str(args.stock)
        os.environ["STOCK_TEST_BUYERS"] = str(args.buyers)
    else:
        os.environ.pop("STOCK_TEST_STOCK", None)
        os.environ.pop("STOCK_TEST_BUYERS", None)
    os.environ["STOCK_TEST_REPEATS"] = str(args.repeats)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    suite = unittest.defaultTestLoader.loadTestsFromNames(
        ["tests.test_finance", "tests.test_stock"]
    )
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    report = {
        "command": " ".join(["python", "-m", "tests.run", *sys.argv[1:]]),
        "parameters": {
            "stock_buyers": [[args.stock, args.buyers]]
            if args.stock is not None
            else [[1, 20], [10, 50]],
            "repeats": args.repeats,
            "reservation_seconds": args.reservation_seconds,
        },
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "tests": result.testsRun,
        "success": result.wasSuccessful(),
        "seconds": round(time.monotonic() - started, 2),
        "rows": result.rows,
    }
    root = Path(__file__).resolve().parent.parent / "docs"
    root.mkdir(exist_ok=True)
    (root / "acceptance-latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def cell(value):
        return str(value).replace("|", "/").replace("\n", " ")

    lines = [
        "# Фактическая автоматическая приёмка",
        "",
        f"Тестов: {report['tests']}; результат: {'OK' if report['success'] else 'FAILED'}; время: {report['seconds']} сек.",
        "",
        f"Команда: `{report['command']}` (Windows / PostgreSQL, отдельные временные базы).",
        f"Места/покупатели: {report['parameters']['stock_buyers']}. Повторов нагрузки: {args.repeats}. Резерв: {args.reservation_seconds} с.",
        "ID ниже относятся к изолированной базе конкретного сценария.",
        "",
        "| Сценарий | Ожидалось | Получено | Статус | Заказ / платёж |",
        "|---|---|---|---|---|",
    ]
    lines += [
        "| "
        + " | ".join(cell(r[k]) for k in ("scenario", "expected", "actual", "status"))
        + " | "
        + ", ".join(
            f"{o['public_number']} / {o['payment_id'] or '—'}"
            for o in r.get("orders", [])
        )
        + " |"
        for r in result.rows
    ]
    (root / "acceptance-latest.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
