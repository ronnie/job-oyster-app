"""Interview packs: one markdown page per company, stories checked against the record, debrief lines checked
against the notes."""
import asyncio
from datetime import date

from jobpipe.config import ModelCfg
from jobpipe.llm import AgentResult
from jobpipe import interview_prep


RECORD = "# Impact record\n\n## Northwind\n\nLed 22 teams through the payments migration.\n"
PACK = """### Likely questions

- How did the payments migration go?
  - Story: Northwind, "Led 22 teams through the payments migration."
- Where did the $9 million come from?
  - Story: "Saved $9 million overnight."

### Stories to cite

- Northwind: "Led 22 teams through the payments migration."
- Elsewhere: "Saved $9 million overnight."

### Questions to ask them

- Who owns the on-call rotation?

### Debrief

*Ignore this. The app writes the debrief prompt itself.*
"""


class Canned:
    def __init__(self, files):
        self.files, self.calls = files, []

    async def run(self, call):
        self.calls.append(call)
        if call.label not in self.files:
            raise AssertionError(call.label)
        return AgentResult(files=self.files[call.label], summary="")


def test_a_page_holds_one_pack_per_role_and_keeps_the_rest():
    page = "# Acme\n\nA note I wrote.\n"
    first = interview_prep.assemble("Staff TPM", "Built Oct 27, 2026 from the posting, 1 gap, and no résumé on file.",
                                    {"Likely questions": "- How big was the team?", "Stories to cite": "",
                                     "Questions to ask them": "- Who is on the team?", "Debrief": "nope"},
                                    RECORD, "")
    page = interview_prep.upsert_role(page, "Acme", "acme-staff-tpm", "Staff TPM", first)
    assert page.startswith("# Acme\n\nA note I wrote.")
    assert "### Debrief" in page and "nope" not in page and interview_prep.DEBRIEF_NOTE in page
    second = "## PM\n\nBuilt later.\n\n### Likely questions\n\n- One\n"
    page = interview_prep.upsert_role(page, "Acme", "acme-pm", "PM", second)
    again = interview_prep.upsert_role(page, "Acme", "acme-staff-tpm", "Staff TPM", first.replace("How big", "How large"))
    assert "A note I wrote." in again and "How large was the team?" in again and "## PM" in again
    assert again.count("<!-- role:acme-staff-tpm -->") == 1
    # The editor dropped the markers: the ## title still finds the pack.
    bare = "# Acme\n\n## Staff TPM\n\nOld pack.\n\n## PM\n\nKeep.\n"
    replaced = interview_prep.upsert_role(bare, "Acme", "acme-staff-tpm", "Staff TPM", first)
    assert "Old pack." not in replaced and "Keep." in replaced and "<!-- role:acme-staff-tpm -->" in replaced


def test_cited_stories_are_checked_against_the_record():
    section = interview_prep.assemble("Staff TPM", "Built Oct 27, 2026 from the posting, no gaps on file, and the résumé sent (a.pdf).",
                                      interview_prep.parse_pack(PACK), RECORD, "The résumé says nothing about millions.")
    assert "Led 22 teams through the payments migration." in section
    assert section.count("that quote isn't in the impact record") == 2
    assert section.count("9 isn't in the impact record or the résumé sent") == 2
    assert "check it." in section
    # A figure that is on the résumé sent is not a problem.
    ok = interview_prep.flag_citations('- Northwind: "Saved $9 million overnight."', RECORD, "Saved $9 million.", stories=True)
    assert "9 isn't" not in ok and "that quote isn't" in ok


def test_debrief_notes_skip_the_prompt_and_lines_keep_only_figures_you_wrote():
    section = "## Staff TPM\n\n### Debrief\n\n" + interview_prep.DEBRIEF_NOTE + "\n\nThey asked about onboarding. I said it went from 6 weeks to 2.\n"
    notes = interview_prep.debrief_notes(section)
    assert notes.startswith("They asked") and "Draft lines" not in notes
    assert interview_prep.debrief_notes("## Staff TPM\n\n### Likely questions\n\n- Hi\n") == ""
    lines = interview_prep.bullet_lines("- Cut onboarding from 6 weeks to 2.\n- Saved $400K a year.\n- none\nNot a bullet\n", notes)
    assert [(l["text"], l["ok"]) for l in lines] == [("Cut onboarding from 6 weeks to 2.", True), ("Saved $400K a year.", False)]


def test_the_pack_is_built_from_the_posting_the_gaps_and_the_resume(tmp_path):
    runner = Canned({"interview-prep": {"pack.md": PACK}})
    built = asyncio.run(interview_prep.build_pack(
        runner, ModelCfg("claude-sonnet-5-5", "medium"), company="Acme", title="Staff TPM",
        posting="Own the platform roadmap.", gaps=["AI evaluation"], resume="Led 22 teams.", resume_name="Acme.pdf",
        record=RECORD, brief=interview_prep.PACK_BRIEF, day=date(2026, 10, 27)))
    assert built["ok"] and "Own the platform roadmap." in runner.calls[0].documents["posting.md"]
    assert "AI evaluation" in runner.calls[0].documents["gaps.md"]
    assert runner.calls[0].documents["sent-resume.md"] == "Led 22 teams."
    assert built["markdown"].startswith("## Staff TPM\n")
    assert "Built Oct 27, 2026 from the posting, 1 gap, and the résumé sent (Acme.pdf)." in built["markdown"]
    assert interview_prep.DEBRIEF_NOTE in built["markdown"]

    empty = asyncio.run(interview_prep.build_pack(
        runner, ModelCfg("m", "low"), company="Acme", title="PM", posting="", gaps=[], resume="", resume_name="",
        record=RECORD, brief="x"))
    assert not empty["ok"] and not runner.calls[1:] and "nothing was on file" in empty["markdown"]
    assert "### Questions to ask them" in empty["markdown"]

    runner.files["interview-prep"] = {"pack.md": "I refuse."}
    bad = asyncio.run(interview_prep.build_pack(
        runner, ModelCfg("m", "low"), company="Acme", title="PM", posting="A posting.", gaps=["A gap"], resume="",
        resume_name="", record=RECORD, brief="x"))
    assert not bad["ok"] and "A gap" in bad["markdown"] and "the reply had no pack" in bad["markdown"]


def test_company_pages_remember_what_they_replaced(tmp_path):
    pages = interview_prep.Pages(tmp_path)
    assert pages.read_index() == {} and pages.read("acme") == ""
    pages.write("acme", "# Acme\n\nFirst.\n")
    pages.write("acme", "# Acme\n\nSecond.\n", "# Acme\n\nFirst.\n")
    assert pages.read("acme") == "# Acme\n\nSecond.\n"
    snaps = list((tmp_path / "interview-prep" / "history" / "acme").glob("*.md"))
    assert [s.read_text() for s in snaps] == ["# Acme\n\nFirst.\n"]
    pages.write("acme", "# Acme\n\nThird.\n")          # inside the same stretch: still one snapshot
    assert len(list(snaps[0].parent.glob("*.md"))) == 1
    assert not interview_prep.valid_slug("../secrets") and not pages.known("../secrets")
