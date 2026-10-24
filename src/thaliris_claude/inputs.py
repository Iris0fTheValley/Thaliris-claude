"""Runner payload inputs: tolerant file decoding and typed flag forms.

The flag forms build the same payload a `--json` / `--input` call would carry and
hand it to the same code path, so Core validates it exactly as before. Nothing here
grants authority, supplies provenance or relaxes a refusal.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re

from thaliris import authority as core_authority, core

_LABEL = re.compile(r"[A-Za-z0-9_.:-]+")
_SIMPLE = re.compile(r"[A-Za-z0-9_.-]+")
_OUTCOMES = ("satisfied", "unknown", "repair", "no-current-impact")
_KIND_PREFIX = {"decision": "D", "fact": "F", "progress": "P", "open-item": "OI"}


# ------------------------------------------------------------------ file input

def decode_text(raw: bytes, label: str) -> str:
    """Decode a payload or contract file: UTF-8, UTF-8 with BOM or UTF-16 with BOM (Core's decoder and error)."""
    return core.decode_input_bytes(raw, label)


def read_text(path: str | Path) -> str:
    return decode_text(Path(path).read_bytes(), str(path))


# ------------------------------------------------------------------ flag forms

# command -> flag destinations that select the flag form (argparse dest names)
FLAGS: dict[str, tuple[str, ...]] = {
    "judge": ("requirement", "candidate", "outcome", "observations", "reasoning", "judgment_refs", "judgment_id", "judgment"),
    "register": ("requirement",),
    "observe": ("text", "source", "observation_id", "requirement_refs"),
    "task-update": ("record", "supersedes", "milestone", "active_work", "pending", "clear_active_work", "clear_pending"),
    "task-start": ("human_instruction", "boundary", "invariants", "acceptance", "execution_mode", "execution_constraint"),
}


def add_flags(command: argparse.ArgumentParser, name: str) -> None:
    add = command.add_argument
    if name == "judge":
        add("--requirement", help="flag form: the requirement id this judgment is about")
        add("--candidate", help="flag form: the candidate id")
        add("--outcome", choices=_OUTCOMES, help="flag form")
        add("--observations", help="flag form: comma-separated observation ids (a bare id such as O1 is accepted when exactly one observation of this candidate, scoped to this requirement, ends in it)")
        add("--reasoning", help="flag form: why the outcome follows from those observations")
        add("--judgment-refs", dest="judgment_refs", help="flag form: comma-separated earlier judgment ids this one addresses")
        add("--id", dest="judgment_id", help="flag form: judgment id (default: the next free J<n>)")
        add("--judgment", action="append", metavar="REQ|OUTCOME|OBS[,OBS]|REASONING",
            help="flag form, batch (repeatable, atomic, one revision; needs --candidate; instead of the single-judgment flags)")
    elif name == "register":
        add("--requirement", action="append", metavar="ID=WORKCHAIN_REF", help="flag form (repeatable): register one requirement")
    elif name == "observe":
        add("--text", help="flag form: the observation")
        add("--source", action="append", metavar="LOCATOR", help="flag form (repeatable): a source locator, recorded verbatim")
        add("--id", dest="observation_id", help="flag form: observation id (default: the next free O<n> for the handoff)")
        add("--requirement-refs", dest="requirement_refs", help="flag form: comma-separated requirement ids (default: the handoff's)")
    elif name == "task-update":
        add("--record", action="append", metavar="[ID=]KIND:TEXT", help="flag form (repeatable): append a record; the id is assigned when omitted")
        add("--supersedes", action="append", metavar="OLD[,OLD] | NEWID=OLD[,OLD]",
            help="flag form (repeatable): earlier record ids the new record supersedes; with several --record use NEWID=OLD")
        add("--milestone", help="flag form: set the current milestone")
        add("--active-work", dest="active_work", action="append", help="flag form (repeatable): REPLACES Active Work with the items given")
        add("--pending", action="append", help="flag form (repeatable): REPLACES Pending Results with the items given")
        add("--clear-active-work", dest="clear_active_work", action="store_true", default=None, help="flag form: empty Active Work")
        add("--clear-pending", dest="clear_pending", action="store_true", default=None, help="flag form: empty Pending Results")
    elif name == "task-start":
        add("--human-instruction", dest="human_instruction", help="flag form: the user's instruction, quoted faithfully")
        add("--boundary", help="flag form: what is in and out of scope")
        add("--invariants", help="flag form: hard constraints that must hold")
        add("--acceptance", help="flag form: how we will know the request is met")
        add("--execution-mode", dest="execution_mode", choices=core_authority.MODES, help="flag form")
        add("--execution-constraint", dest="execution_constraint", help="flag form, optional: claude-model:<alias>")


def flag_form(args, name: str) -> bool:
    return any(getattr(args, dest, None) is not None for dest in FLAGS[name])


def _need(args, name: str, required: tuple[tuple[str, str], ...]) -> None:
    missing = [flag for dest, flag in required if not getattr(args, dest, None)]
    if missing:
        raise ValueError(f"FLAG_FORM_INCOMPLETE: `{name}` in flag form also needs {', '.join(missing)}")


def used_ids(state: dict) -> set[str]:
    """Every task object id Core refuses to reuse."""
    ids: set[str] = set()
    for field in ("records", "evidence_refs", "artifact_refs", "verification_results"):
        ids |= {str(item["id"]) for item in state.get(field, []) if isinstance(item, dict) and "id" in item}
    for items in (state.get("evidence_continuity") or {}).values():
        ids |= {str(item["id"]) for item in items if isinstance(item, dict) and "id" in item}
    return ids


def next_free(ids: set[str], prefix: str, stem: str) -> str:
    """The smallest `<stem><n>` with `<prefix><stem><n>` unused, reserved in `ids`."""
    number = 1
    while f"{prefix}{stem}{number}" in ids:
        number += 1
    ids.add(f"{prefix}{stem}{number}")
    return f"{stem}{number}"


def _names(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _label(value: str, what: str) -> str:
    if not _LABEL.fullmatch(value):
        raise ValueError(f"INVALID_ID: {what} `{value}` may use only letters, digits and `_ . : -`")
    return value


def _resolve_refs(names: list[str], observations: list[dict], candidate: str | None, requirement: str | None) -> list[str]:
    """A bare observation id such as O1 resolves to the one handoff-qualified id (`H1a2b3c4d.O1`) it is a suffix of.

    Only observations of the judged candidate that are not scoped to other requirements are eligible; with zero or several
    eligible matches the bare id is left for Core to refuse, with its suggestions.
    """
    known = {str(item["id"]) for item in observations}
    refs = []
    for ref in names:
        matches = [str(item["id"]) for item in observations
                   if str(item["id"]).endswith("." + ref) and item.get("candidate") == candidate
                   and (not item.get("requirement_refs") or requirement in item["requirement_refs"])]
        refs.append(ref if ref in known or len(matches) != 1 else matches[0])
    return refs


def _judge(args, state: dict) -> dict:
    known = [item for item in (state.get("evidence_continuity") or {}).get("observations", []) if isinstance(item, dict) and "id" in item]
    if args.judgment:
        single = [flag for dest, flag in (("requirement", "--requirement"), ("outcome", "--outcome"), ("observations", "--observations"),
                                          ("reasoning", "--reasoning"), ("judgment_id", "--id"), ("judgment_refs", "--judgment-refs"))
                  if getattr(args, dest, None)]
        if single:
            raise ValueError(f"CONFLICTING_FLAGS: --judgment (batch) cannot be combined with {', '.join(single)}")
        _need(args, "judge", (("candidate", "--candidate"),))
        ids, judgments = used_ids(state), []
        for spec in args.judgment:
            parts = [part.strip() for part in spec.split("|", 3)]
            if len(parts) != 4 or not all(parts) or parts[1] not in _OUTCOMES:
                raise ValueError("INVALID_JUDGMENT: use REQ|OUTCOME|OBS[,OBS]|REASONING with OUTCOME one of " + ", ".join(_OUTCOMES))
            judgments.append({"id": next_free(ids, "", "J"), "candidate": args.candidate, "requirement_ref": parts[0],
                              "observation_refs": _resolve_refs(_names(parts[2]), known, args.candidate, parts[0]), "outcome": parts[1], "reasoning": parts[3]})
        return {"judgments": judgments}
    _need(args, "judge", (("requirement", "--requirement"), ("candidate", "--candidate"), ("outcome", "--outcome"),
                          ("observations", "--observations"), ("reasoning", "--reasoning")))
    payload = {"id": _label(args.judgment_id, "judgment id") if args.judgment_id else next_free(used_ids(state), "", "J"),
               "candidate": args.candidate, "requirement_ref": args.requirement,
               "observation_refs": _resolve_refs(_names(args.observations), known, args.candidate, args.requirement),
               "outcome": args.outcome, "reasoning": args.reasoning}
    if args.judgment_refs:
        payload["judgment_refs"] = _names(args.judgment_refs)
    return payload


def _register(args) -> dict:
    requirements = []
    for item in args.requirement or []:
        identifier, separator, reference = item.partition("=")
        if not separator or not identifier.strip() or not reference.strip():
            raise ValueError(f"INVALID_REQUIREMENT: `{item}` must look like ID=WORKCHAIN_REF")
        requirements.append({"id": identifier.strip(), "workchain_ref": reference.strip()})
    return {"requirements": requirements}


def _source_kind(locator: str) -> str:
    return "url" if re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", locator) else "local"


def _observe(args, state: dict, prefix: str, *, qualified: bool) -> dict:
    """`prefix` is the handoff namespace; `qualified` means ids must carry it (Controller import)."""
    _need(args, "observe", (("text", "--text"), ("source", "--source")))
    ids, lead = used_ids(state), prefix if qualified else ""
    if args.observation_id:
        identifier = _label(args.observation_id, "observation id")
    else:
        identifier = lead + next_free(ids, prefix, "O")
    sources = []
    summary = args.text.strip()[:300]
    for locator in args.source:
        if not locator.strip():
            raise ValueError("INVALID_SOURCE: --source needs a locator")
        sources.append({"id": lead + next_free(ids, prefix, "S"), "kind": _source_kind(locator), "locator": locator, "summary": summary})
    payload = {"id": identifier, "text": args.text, "source_refs": [item["id"] for item in sources], "sources": sources}
    if args.requirement_refs:
        payload["requirement_refs"] = _names(args.requirement_refs)
    return payload


def _split_record(spec: str) -> tuple[str | None, str, str]:
    colon, equals = spec.find(":"), spec.find("=")
    identifier = None
    if equals != -1 and (colon == -1 or equals < colon):
        identifier, spec = spec[:equals].strip(), spec[equals + 1:]
        if not _SIMPLE.fullmatch(identifier):
            raise ValueError(f"INVALID_RECORD: id `{identifier}` may use only letters, digits and `_ . -`")
    kind, separator, text = spec.partition(":")
    if not separator or not _SIMPLE.fullmatch(kind.strip()) or not text.strip():
        raise ValueError("INVALID_RECORD: use [ID=]KIND:TEXT, for example decision:keep the parser")
    return identifier, kind.strip(), text.strip()


def _task_update(args, state: dict) -> dict:
    payload: dict = {}
    if args.record:
        ids, records = used_ids(state), []
        parsed = [_split_record(item) for item in args.record]
        ids |= {identifier for identifier, _, _ in parsed if identifier}
        explicit = [identifier for identifier, _, _ in parsed if identifier]
        if len(explicit) != len(set(explicit)):
            raise ValueError("INVALID_RECORD: duplicate record id in one call")
        taken = used_ids(state)
        if any(identifier in taken for identifier in explicit):
            raise ValueError("INVALID_RECORD: a record with that id already exists; append a new record that supersedes it")
        for identifier, kind, text in parsed:
            if identifier is None:
                stem = _KIND_PREFIX.get(kind, "R")
                identifier = next_free(ids, "", stem)
            records.append({"id": identifier, "kind": kind, "text": text, "source_refs": []})
        supersedes = args.supersedes or []
        if supersedes:
            targets = {item["id"]: item for item in records}
            for spec in supersedes:
                owner, separator, olds = spec.partition("=")
                if separator:
                    if owner not in targets or not any(i == owner for i, _, _ in parsed):
                        raise ValueError(f"INVALID_SUPERSEDES: `{owner}` must be a record id given explicitly with --record in this call")
                    holder = targets[owner]
                else:
                    if len(records) != 1:
                        raise ValueError("INVALID_SUPERSEDES: with several --record use NEWID=OLD[,OLD]")
                    holder, olds = records[0], spec
                holder.setdefault("supersedes", []).extend(_names(olds))
        payload["records"] = records
    elif args.supersedes:
        raise ValueError("INVALID_SUPERSEDES: --supersedes belongs to a --record")
    if args.milestone is not None:
        payload["current_milestone"] = args.milestone
    for dest, clear, field in (("active_work", "clear_active_work", "active_work"), ("pending", "clear_pending", "pending_results")):
        items, cleared = getattr(args, dest), getattr(args, clear)
        if items and cleared:
            raise ValueError(f"CONFLICTING_FLAGS: --{dest.replace('_', '-')} and --{clear.replace('_', '-')} together")
        if items:
            payload[field] = list(items)
        elif cleared:
            payload[field] = []
    if not payload:
        raise ValueError("FLAG_FORM_EMPTY: `task-update` flag form needs at least one of --record, --milestone, --active-work, "
                         "--pending, --clear-active-work, --clear-pending")
    return payload


def _contract(args) -> dict:
    _need(args, "task-start", (("human_instruction", "--human-instruction"), ("boundary", "--boundary"),
                               ("invariants", "--invariants"), ("acceptance", "--acceptance"),
                               ("execution_mode", "--execution-mode")))
    contract = {"human_instruction": args.human_instruction, "boundary": args.boundary, "invariants": args.invariants,
                "acceptance": args.acceptance, "execution_mode": args.execution_mode}
    if args.execution_constraint is not None:
        contract["execution_constraint"] = args.execution_constraint
    return contract


def build(args, name: str, state: dict | None = None, *, prefix: str = "", qualified: bool = False) -> dict:
    """Build the payload (or, for task-start, the contract) a flag-form call stands for."""
    if name == "register":
        return _register(args)
    if name == "task-start":
        return _contract(args)
    state = state or {}
    if name == "judge":
        return _judge(args, state)
    if name == "observe":
        return _observe(args, state, prefix, qualified=qualified)
    if name == "task-update":
        return _task_update(args, state)
    raise ValueError(f"no flag form for {name}")
