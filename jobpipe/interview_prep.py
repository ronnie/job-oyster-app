"""Interview prep: one markdown page per company, built when a role reaches Interviewing.

The notes page (data/interview-prep.md) stays for anything that isn't about one company. A company's page
(data/interview-prep/companies/<slug>.md) holds a pack per role: likely questions, stories cited from the
impact record, questions to ask them, and a debrief. The pack is written from that role's posting, its gaps
and the résumé that was sent. A quote or a figure in a cited story that isn't in the record is marked, the
way a setup-interview draft marks a figure the answers didn't give.

The debrief is the candidate's own notes of what was asked and what they said. Drafted lines are checked the
same way, and nothing reaches the impact record until they add it (record_import.apply, "From your interviews").
"""
from __future__ import annotations

import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import evidence
from .llm import AgentCall, Runner
from .config import ModelCfg
from .store import _write, slug

SECTIONS = ("Likely questions", "Stories to cite", "Questions to ask them", "Debrief")
DEBRIEF_NOTE = ("*After the interview, write what they asked and what you said, in your own words. "
                "Draft lines from these notes, then tick the ones that are true: those go into your impact record.*")

PACK_BRIEF = """You are preparing {first} for one interview. The posting, the gaps from the fit report, and the résumé {first} sent are in the documents when the app has them. {first}'s impact record and other résumés are in your materials.

Write pack.md and nothing else. Use exactly these four headings, in this order, each a markdown heading of three hashes:

### Likely questions
### Stories to cite
### Questions to ask them
### Debrief

Likely questions: 5 to 8 bullets ("- "). Draw them from the posting and the gaps. Questions a gap would draw come first. Under each bullet, one indented line: "  - Story: " then the impact-record section name and a short quote that appears in that section, in quotation marks. If the record has nothing for it, write "  - Story: none in the impact record yet."

Stories to cite: 3 to 6 bullets. Each names an impact-record section and quotes one line from it, word for word, in quotation marks. Use a story only for something the posting or the gaps ask about. Never invent a number, tool, team size or outcome, and never quote a line that is not in the impact record.

Questions to ask them: 4 to 6 bullets, specific to this posting (the work, the team, a gap). Not generic interview questions.

Debrief: write exactly this one italic line and nothing else: *After the interview, write what they asked and what you said, in your own words. Draft lines from these notes, then tick the ones that are true: those go into your impact record.*

The résumé they sent is what an interviewer has read, so a question may refer to a line on it. A story still has to come from the impact record, not from a line that exists only on the résumé. If a document is missing, say so in one line under the heading it affects and carry on. Do not refuse the whole pack."""

DEBRIEF_BRIEF = """You read {first}'s notes from a real interview for one role. Write the lines those notes add to {first}'s impact record.

Write lines.md: a bullet list ("- " each), 3 to 8 lines, each one result or responsibility in {first}'s own facts, in résumé style (an action verb first, no "I"). Use only what the debrief notes say: never add a number, tool, team size or outcome they don't state. Keep each figure exactly as written, and keep "about" or "roughly" when the notes said it. Leave out anything the impact record and résumés in your materials already say. If the notes give nothing new, write "- none"."""

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
_QUOTE = re.compile(r"[“\"]([^”\"]{8,})[”\"]")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def company_slug(company: str) -> str:
    return slug(company, max_len=80)


def role_key(company: str, title: str) -> str:
    return slug(company, title, max_len=80)


def valid_slug(s: str) -> bool:
    return bool(_SLUG.fullmatch(s or ""))


def source_line(day: date, *, posting: bool, gaps: int, resume_name: str) -> str:
    """The line under a role's heading: what the pack was built from."""
    posting_s = "the posting" if posting else "no posting on file"
    gaps_s = f"{gaps} gap" + ("" if gaps == 1 else "s") if gaps else "no gaps on file"
    resume_s = f"the résumé sent ({resume_name})" if resume_name else "no résumé on file"
    return f"Built {day:%b} {day.day}, {day.year} from {posting_s}, {gaps_s}, and {resume_s}."


def placeholder(title: str) -> str:
    return f"## {title}\n\nWriting the pack from the posting, the gaps and the résumé you sent.\n"


def _marked(key: str, body: str) -> str:
    return f"<!-- role:{key} -->\n{body.strip()}\n<!-- /role:{key} -->\n"


def upsert_role(page: str, company: str, key: str, title: str, body: str) -> str:
    """The page with this role's pack replaced, or appended. Notes and other roles stay as they are.

    The pack is wrapped in markers. A page the editor saved without them is found by its ## title."""
    block = _marked(key, body)
    if not page.strip():
        page = f"# {company}\n"
    pat = re.compile(rf"<!-- role:{re.escape(key)} -->\n.*?<!-- /role:{re.escape(key)} -->\n?", re.S)
    if pat.search(page):
        return pat.sub(lambda _m: block, page, count=1).rstrip() + "\n"
    head = re.compile(rf"^##\s+{re.escape(title)}\s*$", re.M)
    found = head.search(page)
    if found:
        nxt = re.search(r"^##\s+", page[found.end():], re.M)
        end = found.end() + nxt.start() if nxt else len(page)
        return (page[:found.start()].rstrip() + "\n\n" + block + "\n" + page[end:].lstrip()).rstrip() + "\n"
    return page.rstrip() + "\n\n" + block


def role_section(page: str, key: str, title: str) -> str:
    """The markdown of one role's pack, markers aside, or "" if it isn't on the page."""
    m = re.search(rf"<!-- role:{re.escape(key)} -->\n(.*?)<!-- /role:{re.escape(key)} -->", page, re.S)
    if m:
        return m.group(1).strip()
    head = re.compile(rf"^##\s+{re.escape(title)}\s*$", re.M)
    found = head.search(page)
    if not found:
        return ""
    nxt = re.search(r"^##\s+", page[found.end():], re.M)
    end = found.end() + nxt.start() if nxt else len(page)
    return page[found.start():end].strip()


def debrief_notes(section: str) -> str:
    """What the candidate wrote under ### Debrief, without the app's instruction line."""
    m = re.search(r"^### Debrief\s*\n(.*)\Z", section, re.S | re.M)
    if not m:
        return ""
    rest = m.group(1).strip()
    rest = re.sub(r"^\*[^*]*\*\s*", "", rest, count=1).strip()
    if rest.startswith("After the interview, write what they asked"):
        rest = rest.split("\n", 1)[1].strip() if "\n" in rest else ""
    return rest


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def flag_citations(text: str, record: str, resume: str, *, stories: bool) -> str:
    """Mark a cited story whose quote, or whose figure, isn't in the impact record (a figure may be on the résumé sent).

    `stories` flags every bullet (the Stories to cite section). Otherwise only a line that cites a story."""
    have = set(evidence.figures(record + "\n" + resume))
    norm = _norm(record)
    out = []
    for line in text.splitlines():
        cites = stories or "story:" in line.lower()
        notes = []
        if cites and _BULLET.match(line):
            for quote in _QUOTE.findall(line):
                if _norm(quote) not in norm:
                    notes.append("that quote isn't in the impact record")
                    break
            missing = [f for f in evidence.figures(line) if f not in have]
            if missing:
                notes.append(f"{', '.join(missing)} isn't in the impact record or the résumé sent")
        if notes and "check it" not in line:
            line = line.rstrip() + " — " + "; ".join(dict.fromkeys(notes)) + "; check it."
        out.append(line)
    return "\n".join(out)


def parse_pack(md: str) -> Optional[dict[str, str]]:
    """The four sections of a model's pack.md, or None if it didn't write them."""
    if not md or not re.search(r"^###\s+Likely questions\s*$", md, re.M):
        return None
    out = {}
    for name in SECTIONS:
        m = re.search(rf"^###\s+{re.escape(name)}\s*\n(.*?)(?=^###\s|\Z)", md, re.S | re.M)
        out[name] = (m.group(1).strip() if m else "")
    return out


def assemble(title: str, source: str, sections: dict[str, str], record: str, resume: str) -> str:
    """One role's pack, with cited stories checked against the impact record."""
    sections = dict(sections)
    sections["Likely questions"] = flag_citations(sections.get("Likely questions") or "", record, resume, stories=False)
    sections["Stories to cite"] = flag_citations(sections.get("Stories to cite") or "", record, resume, stories=True)
    sections["Debrief"] = DEBRIEF_NOTE
    defaults = {
        "Likely questions": "- (None yet. Add the questions you expect.)",
        "Stories to cite": "- (None cited yet. Add a story from your impact record.)",
        "Questions to ask them": "- (None yet.)",
    }
    out = [f"## {title}", "", source, ""]
    for name in SECTIONS:
        body = (sections.get(name) or "").strip() or defaults.get(name, "")
        out += [f"### {name}", "", body, ""]
    return "\n".join(out).rstrip() + "\n"


def fallback(title: str, source: str, gaps: list[str], reason: str) -> str:
    """A pack with the gaps on file when the model doesn't answer, so the page still has the four headings."""
    questions = "\n".join(f"- {g}" for g in gaps) or "- (No gaps were on file. Read the posting and write the questions you expect.)"
    sections = {
        "Likely questions": questions + f"\n\nThe pack could not be written ({reason}). Edit it, or build it again.",
        "Stories to cite": "- (None cited yet. Add a story from your impact record under each question.)",
        "Questions to ask them": "- (None yet.)",
        "Debrief": DEBRIEF_NOTE,
    }
    return assemble(title, source, sections, "", "")


def bullet_lines(md: str, said: str) -> list[dict]:
    """Drafted bullets as {"text", "ok", "why"}. A figure the source text doesn't contain is left unticked."""
    have = set(evidence.figures(said))
    out = []
    for raw in (md or "").splitlines():
        text = _BULLET.sub("", raw).strip()
        if not text or text.lower().rstrip(".") == "none":
            continue
        if not _BULLET.match(raw):
            continue
        missing = [f for f in evidence.figures(text) if f not in have]
        out.append({"text": text, "ok": not missing,
                    "why": (f"{', '.join(missing)} isn't in your notes: check it." if missing else "")})
    return out


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text[:n] if len(text) > n else text


async def build_pack(runner: Runner, model: ModelCfg, *, company: str, title: str, posting: str,
                     gaps: list[str], resume: str, resume_name: str, record: str, brief: str,
                     day: Optional[date] = None) -> dict:
    """One role's pack. The model is asked only when there is a posting, a gap or a résumé to read;
    a failure still returns a pack built from the gaps, with ok False."""
    day = day or date.today()
    posting, resume = _clip(posting, 20000), _clip(resume, 12000)
    gaps = [g.strip() for g in gaps if g and g.strip()][:20]
    source = source_line(day, posting=bool(posting), gaps=len(gaps), resume_name=resume_name if resume else "")
    if not posting and not gaps and not resume:
        md = fallback(title, source, [], "nothing was on file for this role")
        return {"markdown": md, "sources": source, "ok": False, "error": "nothing was on file for this role"}
    docs = {}
    if posting:
        docs["posting.md"] = posting
    if gaps:
        docs["gaps.md"] = "\n".join(f"- {g}" for g in gaps)
    if resume:
        docs["sent-resume.md"] = resume
    missing = [name for name, have in (("posting", posting), ("gaps", gaps), ("résumé sent", resume)) if not have]
    tail = f"Role: {title} at {company}."
    if missing:
        tail += " Not on file: " + ", ".join(missing) + "."
    try:
        res = await runner.run(AgentCall(
            label="interview-prep", model=model, instructions=brief, documents=docs,
            expect=["pack.md"], prose_file="pack.md", tail=tail))
        parsed = parse_pack(res.files.get("pack.md") or "")
    except Exception as e:  # noqa: BLE001 - the page still gets the gaps; the caller shows why
        err = str(e).splitlines()[0][:200] if str(e) else "no reply"
        return {"markdown": fallback(title, source, gaps, err), "sources": source, "ok": False, "error": err}
    if not parsed:
        err = "the reply had no pack"
        return {"markdown": fallback(title, source, gaps, err), "sources": source, "ok": False, "error": err}
    return {"markdown": assemble(title, source, parsed, record, resume), "sources": source, "ok": True, "error": ""}


async def draft_debrief(runner: Runner, model: ModelCfg, *, company: str, title: str, notes: str, brief: str) -> list[dict]:
    """Lines the debrief notes support. The notes have to say something first."""
    notes = notes.strip()
    if not notes:
        return []
    res = await runner.run(AgentCall(
        label="interview-debrief", model=model, instructions=brief,
        documents={"debrief.md": f"Company: {company}\nTitle: {title}\n\n{notes[:8000]}"},
        expect=["lines.md"], prose_file="lines.md",
        tail="Draft lines from the debrief notes only."))
    return bullet_lines(res.files.get("lines.md") or "", notes)


class Pages:
    """The company pages and the index of roles on them. The notes page is not one of these."""

    def __init__(self, root: Path):
        self.root = root
        self.dir = root / "interview-prep"
        self.companies = self.dir / "companies"
        self.index_path = self.dir / "index.json"
        self.history = self.dir / "history"
        self.lock = threading.RLock()

    def read_index(self) -> dict:
        with self.lock:
            try:
                import json
                return json.loads(self.index_path.read_text())
            except (OSError, ValueError):
                return {}

    def write_index(self, data: dict) -> None:
        with self.lock:
            _write(self.index_path, data)

    def path(self, slug_s: str) -> Path:
        if not valid_slug(slug_s):
            raise ValueError(slug_s)
        return self.companies / f"{slug_s}.md"

    def read(self, slug_s: str) -> str:
        with self.lock:
            try:
                return self.path(slug_s).read_text()
            except (OSError, ValueError):
                return ""

    def known(self, slug_s: str) -> bool:
        """A page the app made, or a file already there (so a hand-added one can be edited)."""
        if not valid_slug(slug_s):
            return False
        with self.lock:
            return slug_s in self.read_index() or self.path(slug_s).exists()

    def write(self, slug_s: str, text: str, previous: Optional[str] = None) -> None:
        """Save the page. The text it replaces is kept under history/<slug>, at most one copy a stretch."""
        with self.lock:
            path = self.path(slug_s)
            current = previous if previous is not None else (path.read_text() if path.exists() else "")
            if text == current and path.exists():
                return
            if current:
                hist = self.history / slug_s
                hist.mkdir(parents=True, exist_ok=True)
                kept = sorted(hist.glob("*.md"))
                if not kept or (datetime.now().timestamp() - kept[-1].stat().st_mtime) >= 600:
                    (hist / f"{datetime.now():%Y%m%d-%H%M%S}.md").write_text(current)
                    for old in sorted(hist.glob("*.md"))[:-50]:
                        old.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(text)
            tmp.replace(path)

    def has_role(self, slug_s: str, key: str, title: str) -> bool:
        return bool(role_section(self.read(slug_s), key, title))
