"""Explicit stage timing for a caller-driven recommendation run.

Only this module writes run logs. Nothing here calls MCP, sleeps, retries, or
touches a shell wrapper: a caller decides when a stage starts and ends, and the
log records what was observed. Gaps with no start/end are reported as
``unattributed`` instead of being attributed to a neighbouring stage.

Typical caller usage (the routine itself is not changed by this repo)::

    from engine.stage_timer import StageTimer
    t = StageTimer("runs/timings/lunch-20261110.jsonl", run_label="lunch")
    t.mark_dependency("mcp-wrapper", present=True)
    with t.stage("calculate-price") as checkpoint:
        response = call_mcp()
        # response["datetime"] is an API stamp, not the tool-return clock.
        checkpoint["mark_mcp_returned"](response)
        assemble_snapshots(response)
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
class _OpenStage:
    name: str
    started_mono: float
    mcp_returned_mono: Optional[float] = None
    mcp_returned_wall: Optional[datetime] = None


@dataclass
class StageTimer:
    path: Optional[str] = None
    run_label: str = ""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    clock: Any = time.monotonic
    now: Any = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    _started_at: Optional[float] = None
    _open: List[_OpenStage] = field(default_factory=list)

    def _wall_now(self) -> datetime:
        if self.now is None:
            return _now()
        moment = self.now() if callable(self.now) else self.now
        if not isinstance(moment, datetime):
            return _now()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=CST)
        return moment

    def _emit(self, record: Mapping[str, Any]) -> None:
        row = {
            "runId": self.run_id,
            "runLabel": self.run_label,
            "loggedAt": _iso(self._wall_now()),
            **dict(record),
        }
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
                "observedAt": recorded_at or _iso(self._wall_now()),
            }
        )

    def note(self, message: str, **extra: Any) -> None:
        self._emit({"event": EVENT_NOTE, "message": str(message), **extra})

    def mark_mcp_returned(self, stage: Optional[str] = None, response: Any = None) -> None:
        """Record the monotonic instant an in-flight MCP/tool call returned.

        Call this while ``stage`` is still open, before snapshot assembly.
        ``response`` may be the upstream JSON so the caller can pass it through.
        A ``datetime`` field on that JSON is an API stamp and is not a clock:
        it is never parsed and never becomes ``mcpReturnedAt`` or a duration.
        The first mark for an open stage wins.
        """
        if isinstance(response, Mapping) and "datetime" in response:
            # Accepted and discarded. Do not parse, do not store.
            pass
        target = str(stage or "")
        slot: Optional[_OpenStage] = None
        if target:
            for item in reversed(self._open):
                if item.name == target:
                    slot = item
                    break
        elif self._open:
            slot = self._open[-1]
        if slot is None or slot.mcp_returned_mono is not None:
            return
        slot.mcp_returned_mono = float(self.clock())
        slot.mcp_returned_wall = self._wall_now()

    @contextmanager
    def stage(self, name: str, **extra: Any) -> Iterator[Dict[str, Any]]:
        started = float(self.clock())
        stage_name = str(name)
        slot = _OpenStage(name=stage_name, started_mono=started)
        self._open.append(slot)
        self._emit(
            {
                "event": EVENT_START,
                "stage": stage_name,
                "startedAt": _iso(self._wall_now()),
                **extra,
            }
        )
        info: Dict[str, Any] = {}

        def mark_mcp_returned(response: Any = None) -> None:
            self.mark_mcp_returned(stage_name, response=response)

        info["mark_mcp_returned"] = mark_mcp_returned
        status = "ok"
        try:
            yield info
        except Exception as exc:  # record then re-raise: timing must not hide errors
            status = f"error:{type(exc).__name__}"
            raise
        finally:
            ended = float(self.clock())
            if slot in self._open:
                self._open.remove(slot)
            record: Dict[str, Any] = {
                "event": EVENT_END,
                "stage": stage_name,
                "endedAt": _iso(self._wall_now()),
                "durationSeconds": round(max(0.0, ended - started), 3),
                "status": status,
            }
            if slot.mcp_returned_mono is not None:
                record["rpcDurationSeconds"] = round(max(0.0, slot.mcp_returned_mono - started), 3)
                record["snapshotDurationSeconds"] = round(max(0.0, ended - slot.mcp_returned_mono), 3)
                if slot.mcp_returned_wall is not None:
                    record["mcpReturnedAt"] = _iso(slot.mcp_returned_wall)
            if info.get("detail"):
                record["detail"] = info.get("detail")
            self._emit(record)

    def finish(self, *, status: str = "ok") -> Dict[str, Any]:
        summary = summarize_events(self.events)
        self._emit({"event": "run_end", "status": status, "wallSeconds": summary.get("wallSeconds")})
        return summary


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
            stage_row: Dict[str, Any] = {
                "stage": stage,
                "startedAt": _iso(started) if started else None,
                "endedAt": _iso(moment) if moment else None,
                "durationSeconds": row.get("durationSeconds"),
                "status": row.get("status") or "ok",
            }
            # Split fields come only from an explicit tool-return mark.
            # An upstream ``datetime`` stamp on the same row is not a clock.
            for key in ("rpcDurationSeconds", "snapshotDurationSeconds", "mcpReturnedAt"):
                if key in row and row.get(key) is not None:
                    stage_row[key] = row.get(key)
            stages.append(stage_row)
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
