"""Explicit stage timing for a caller-driven recommendation run.

Only this module writes run logs. Nothing here calls MCP, sleeps, retries, or
touches a shell wrapper: a caller decides when a stage starts and ends, and the
log records what was observed. Gaps with no start/end are reported as
``unattributed`` instead of being attributed to a neighbouring stage.

Typical caller usage (the routine itself is not changed by this repo)::

    from engine.stage_timer import StageTimer
    t = StageTimer("runs/timings/lunch-20261110.jsonl", run_label="lunch")
    t.mark_dependency("mcp-wrapper", present=True)
    with t.stage("query-meals"):
        ...
    t.finish()

CLI (manual instrumentation / inspection only)::

    python3 engine/stage_timer.py summarize runs/timings/lunch-20261110.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

CST = timezone(timedelta(hours=8))

EVENT_START = "stage_start"
EVENT_END = "stage_end"
EVENT_DEPENDENCY = "dependency"
EVENT_NOTE = "note"

# A stage whose neighbours are more than this far apart is reported as a gap
# rather than folded into the next stage.
DEFAULT_GAP_SECONDS = 1.0


def _now() -> datetime:
    return datetime.now(CST)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


@dataclass
class StageTimer:
    path: Optional[str] = None
    run_label: str = ""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    clock: Any = time.monotonic
    events: List[Dict[str, Any]] = field(default_factory=list)
    _started_at: Optional[float] = None

    def _emit(self, record: Mapping[str, Any]) -> None:
        row = {"runId": self.run_id, "runLabel": self.run_label, **_now_iso_row(), **dict(record)}
        self.events.append(row)
        if self.path:
            p = Path(self.path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def mark_dependency(self, name: str, *, present: bool, detail: str = "", recorded_at: Optional[str] = None) -> None:
        """Record whether an external prerequisite existed at run start.

        The caller must not read credentials to answer this; a stat/existence
        check is enough (this repo never stores or inspects wrapper content).
        """
        self._emit(
            {
                "event": EVENT_DEPENDENCY,
                "name": str(name),
                "present": bool(present),
                "detail": str(detail or ""),
                "observedAt": recorded_at or _iso(_now()),
            }
        )

    def note(self, message: str, **extra: Any) -> None:
        self._emit({"event": EVENT_NOTE, "message": str(message), **extra})

    @contextmanager
    def stage(self, name: str, **extra: Any) -> Iterator[Dict[str, Any]]:
        started = self.clock()
        self._emit({"event": EVENT_START, "stage": str(name), "startedAt": _iso(_now()), **extra})
        info: Dict[str, Any] = {}
        status = "ok"
        try:
            yield info
        except Exception as exc:  # record then re-raise: timing must not hide errors
            status = f"error:{type(exc).__name__}"
            raise
        finally:
            ended = self.clock()
            self._emit(
                {
                    "event": EVENT_END,
                    "stage": str(name),
                    "endedAt": _iso(_now()),
                    "durationSeconds": round(max(0.0, ended - started), 3),
                    "status": status,
                    **({"detail": info.get("detail")} if info.get("detail") else {}),
                }
            )

    def finish(self, *, status: str = "ok") -> Dict[str, Any]:
        summary = summarize_events(self.events)
        self._emit({"event": "run_end", "status": status, "wallSeconds": summary.get("wallSeconds")})
        return summary


def _now_iso_row() -> Dict[str, str]:
    return {"loggedAt": _iso(_now())}


def load_events(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _parse(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=CST)
    return value


def summarize_events(
    events: List[Mapping[str, Any]],
    *,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
) -> Dict[str, Any]:
    """Per-stage durations plus explicitly unattributed gaps.

    Only stages with both a start and an end get a duration. Everything else is
    reported under ``unattributed`` and never attributed to a named API.
    """
    starts: Dict[str, datetime] = {}
    stages: List[Dict[str, Any]] = []
    dependencies: List[Dict[str, Any]] = []
    timeline: List[Dict[str, Any]] = []

    for row in events:
        event = str(row.get("event") or "")
        stage = str(row.get("stage") or "")
        if event == EVENT_START and stage:
            moment = _parse(row.get("startedAt"))
            if moment is not None:
                starts[stage] = moment
                timeline.append({"at": moment, "kind": "start", "stage": stage})
        elif event == EVENT_END and stage:
            moment = _parse(row.get("endedAt"))
            started = starts.pop(stage, None)
            if moment is not None:
                timeline.append({"at": moment, "kind": "end", "stage": stage})
            stages.append(
                {
                    "stage": stage,
                    "startedAt": _iso(started) if started else None,
                    "endedAt": _iso(moment) if moment else None,
                    "durationSeconds": row.get("durationSeconds"),
                    "status": row.get("status") or "ok",
                }
            )
        elif event == EVENT_DEPENDENCY:
            dependencies.append(
                {
                    "name": row.get("name"),
                    "present": bool(row.get("present")),
                    "detail": row.get("detail") or "",
                    "observedAt": row.get("observedAt"),
                }
            )
        elif event == EVENT_NOTE:
            timeline.append({"at": _parse(row.get("loggedAt")), "kind": "note", "stage": str(row.get("message") or "")})

    missing_end = [{"stage": name, "startedAt": _iso(moment)} for name, moment in starts.items()]

    timeline = [row for row in timeline if row.get("at") is not None]
    timeline.sort(key=lambda row: row["at"])

    unattributed: List[Dict[str, Any]] = []
    for prev, nxt in zip(timeline, timeline[1:]):
        delta = (nxt["at"] - prev["at"]).total_seconds()
        if delta > gap_seconds and not (prev["kind"] == "start" and nxt["kind"] == "end" and prev["stage"] == nxt["stage"]):
            unattributed.append(
                {
                    "from": _iso(prev["at"]),
                    "to": _iso(nxt["at"]),
                    "durationSeconds": round(delta, 3),
                    "between": [f"{prev['kind']}:{prev.get('stage')}", f"{nxt['kind']}:{nxt.get('stage')}"],
                    "reason": "no stage start/end inside this interval",
                }
            )

    wall: Optional[float] = None
    if timeline:
        wall = round((timeline[-1]["at"] - timeline[0]["at"]).total_seconds(), 3)

    measured = sum(float(row["durationSeconds"]) for row in stages if isinstance(row.get("durationSeconds"), (int, float)))
    return {
        "stages": stages,
        "dependencies": dependencies,
        "missingEnd": missing_end,
        "unattributed": unattributed,
        "measuredStageSeconds": round(measured, 3),
        "wallSeconds": wall,
        "note": "unattributed gaps have no start/end evidence; do not attribute them to a named API or stage",
    }


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Inspect a stage-timing JSONL log (offline; never calls MCP)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sm = sub.add_parser("summarize", help="print per-stage durations and unattributed gaps")
    sm.add_argument("path")
    sm.add_argument("--gap-seconds", type=float, default=DEFAULT_GAP_SECONDS)
    args = ap.parse_args(argv)
    if args.cmd == "summarize":
        events = load_events(args.path)
        if not events:
            print(f"no events: {args.path}", file=sys.stderr)
            return 1
        print(json.dumps(summarize_events(events, gap_seconds=args.gap_seconds), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
