"""
Iteratieve prompt-optimalisatie voor More Deals outreach skills.

Gebruikt dezelfde logica als autoresearch van Karpathy:
  wijzig -> test -> meet -> houd/verwerp

Usage:
  python optimize_skills.py                    # Baseline evaluatie
  python optimize_skills.py --optimize         # Evaluatie + verbetering (3 iteraties)
  python optimize_skills.py --skill first_message --optimize --iterations 5
"""

import os
import re
import json
import shutil
import argparse
from datetime import datetime

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(BASE_DIR, "skills")
BACKUP_DIR = os.path.join(SKILLS_DIR, ".backup")
RESULTS_DIR = os.path.join(BASE_DIR, "optimization_results")

GENERATOR_MODEL = "claude-haiku-4-5-20251001"
EVALUATOR_MODEL = "claude-sonnet-4-20250514"

# ── System prompt (zelfde opbouw als app.py, zonder kennisbasis) ──────────────

SYSTEM_INSTRUCTIONS = """Je bent de **More Deals AI Agent** — een expert sales coach en assistent van het More Deals programma, opgericht door Joey Moreau.

**Jouw rol:**
- Beantwoord vragen over sales, onderhandelen, lead generatie en fundraising
- Geef praktisch, concreet advies op basis van het More Deals programma
- Schrijf outreach berichten, follow-ups en reminders met behulp van je outreach skills
- Spreek Nederlands tenzij de gebruiker Engels spreekt

**Communicatiestijl:**
- Direct en to-the-point
- Gebruik concrete voorbeelden en scripts
- Geef altijd actionable stappen

**Formatting regels:**
- Gebruik NOOIT emoji's in je antwoorden
- Structureer antwoorden duidelijk met markdown"""


def load_skills() -> str:
    if not os.path.exists(SKILLS_DIR):
        return ""
    sections = []
    for filename in sorted(os.listdir(SKILLS_DIR)):
        if filename.endswith(".md"):
            filepath = os.path.join(SKILLS_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    sections.append(f.read())
            except Exception:
                pass
    return "\n\n---\n\n".join(sections)


def build_system_prompt() -> str:
    skills = load_skills()
    if skills:
        skills_section = (
            "\n\n## OUTREACH SKILLS\n\n"
            "Hieronder staan je outreach skills. Gebruik deze wanneer een gebruiker "
            "vraagt om outreach berichten te schrijven. Volg de structuur, regels en "
            "het proces exact op.\n\n"
            f"{skills}"
        )
    else:
        skills_section = ""
    return f"{SYSTEM_INSTRUCTIONS}{skills_section}"


# ── Test cases ────────────────────────────────────────────────────────────────

TEST_CASES = [
    {
        "id": "first_msg_saas_founder",
        "skill": "first_message",
        "skill_file": "skills/first_message.md",
        "user_message": (
            "Schrijf een eerste outreach bericht.\n"
            "1. Doelgroep: SaaS founders die een B2B product verkopen\n"
            "2. Waardepropositie: een CRM tool die pipeline velocity verdubbelt\n"
            "3. Voornaam: Hylke\n"
            "4. Toon: peer/founder"
        ),
    },
    {
        "id": "first_msg_investor",
        "skill": "first_message",
        "skill_file": "skills/first_message.md",
        "user_message": (
            "Schrijf een eerste outreach bericht.\n"
            "1. Doelgroep: Angel investors die actief zijn in early-stage startups\n"
            "2. Waardepropositie: een deal-flow platform voor betere startup scouting\n"
            "3. Voornaam: Hylke\n"
            "4. Toon: cold/professioneel"
        ),
    },
    {
        "id": "first_msg_community_leader",
        "skill": "first_message",
        "skill_file": "skills/first_message.md",
        "user_message": (
            "Schrijf een eerste outreach bericht.\n"
            "1. Doelgroep: Community managers die professionele netwerken runnen\n"
            "2. Waardepropositie: een platform dat leden automatisch matcht op basis van wederzijdse waarde\n"
            "3. Voornaam: Hylke\n"
            "4. Toon: warm/bekend"
        ),
    },
    {
        "id": "second_msg_positive_reply",
        "skill": "second_message",
        "skill_file": "skills/second_message.md",
        "user_message": (
            "Schrijf een tweede bericht (follow-up na reactie).\n"
            '1. De lead antwoordde: "Hi Hylke, klinkt interessant. Vertel me meer."\n'
            "2. Naam lead: Thomas\n"
            "3. Bedrijfsnaam: TechFlow BV\n"
            "4. Mijn voornaam: Hylke"
        ),
    },
    {
        "id": "reminder_basic",
        "skill": "reminders",
        "skill_file": "skills/reminders.md",
        "user_message": "Schrijf follow-up reminders. Naam lead: Alex. Mijn naam: Hylke.",
    },
]

# ── Evaluation rubrics ────────────────────────────────────────────────────────

RUBRICS = {
    "first_message": """Score this outreach message output on these criteria (0-10 each):

1. char_count: Each message variant must be <=220 characters (the actual message text only, not labels/explanations). 10 = all under 220, 0 = multiple over.
2. no_forbidden_placeholders: Only [Name] is allowed. No [sector], [region], [company], [X], etc. 10 = clean, 0 = forbidden placeholders.
3. observable_label: Label references observable facts (profile, title, public activity), not assumptions about personality/motivation/business model. 10 = observable, 0 = assumed.
4. gap_surfacing_question: Question exposes a gap/challenge about systems/processes/outcomes. Not yes/no dead-end. Does not use "ever". Not about feelings/opinions. 10 = strong, 0 = weak.
5. cta_if_not_logic: After gap question, CTA uses "if not" framing (not "if so"/"if yes"). 10 = correct, 0 = inverted logic.
6. tone_compliance: No filler ("amazing", "love what you're doing"). No corporate ("leverage", "synergies"). Sharp, human tone. 10 = perfect, 0 = bot/corporate.
7. structure_compliance: Follows Hi [Name], personal1, Label, Question, CTA, Best, name structure. 10 = correct, 0 = wrong.
8. three_variants: Exactly 3 variants produced. 10 = yes, 0 = wrong count.

Return ONLY valid JSON:
{
  "scores": {"char_count": N, "no_forbidden_placeholders": N, "observable_label": N, "gap_surfacing_question": N, "cta_if_not_logic": N, "tone_compliance": N, "structure_compliance": N, "three_variants": N},
  "total": N,
  "max_total": 80,
  "failures": ["specific rule violations"],
  "suggestions": ["specific improvements to the skill prompt that would fix these failures"]
}""",

    "second_message": """Score this second outreach message output on these criteria (0-10 each):

1. label_human: Acknowledges lead's reply naturally, mirrors energy, no filler like "Geweldig!" or "Super!". 10 = human, 0 = filler.
2. long_story_short: Starts with transition phrase. Does NOT name company/product. Creates intrigue via journey arc. 3-5 sentences. 10 = perfect, 0 = names product or too long.
3. homerun_bridge: References both their reply AND [BEDRIJF]. Shows homework. One-two sentences. Not vague. 10 = grounded bridge, 0 = generic.
4. cta_or_not: Includes "of niet"/"or not" framing. Offers specific availability. Ends human. Not "Wanneer heb jij tijd?". 10 = correct, 0 = missing safety valve.
5. email_format: Full paragraphs, blank lines between blocks, subject line <=5 words. 3 variants. 10 = correct, 0 = wrong format.
6. linkedin_split_send: Split into 4 separate messages with --- SEND --- between them. 3 variants. 10 = correct, 0 = wall of text.
7. tone: Sharp operator, not salesperson. No filler. Short sentences. 10 = perfect, 0 = corporate/filler.
8. three_variants_per_format: 3 email + 3 linkedin variants. 10 = yes, 0 = wrong count.

Return ONLY valid JSON:
{
  "scores": {"label_human": N, "long_story_short": N, "homerun_bridge": N, "cta_or_not": N, "email_format": N, "linkedin_split_send": N, "tone": N, "three_variants_per_format": N},
  "total": N,
  "max_total": 80,
  "failures": ["specific rule violations"],
  "suggestions": ["specific improvements to the skill prompt"]
}""",

    "reminders": """Score this reminder output on these criteria (0-10 each):

1. r1_char_limit: Reminder 1 variants are all <=120 characters. 10 = all under, 0 = over.
2. r1_binary_framing: Reminder 1 offers a binary choice (missed it / not relevant). 10 = clear binary, 0 = no binary.
3. r1_no_repitch: Reminder 1 does NOT re-pitch the product or restate value prop. 10 = clean, 0 = re-pitches.
4. r2_char_limit: Reminder 2 variants are all <=160 characters. 10 = all under, 0 = over.
5. r2_shelf_signal: Reminder 2 signals putting conversation on the shelf. 10 = clear shelf signal, 0 = missing.
6. r2_open_door: Reminder 2 leaves door open for future without begging. 10 = graceful, 0 = desperate or closed.
7. tone: Casual, peer-level, no pressure, no corporate. Not passive-aggressive. 10 = perfect, 0 = wrong tone.
8. three_variants_each: 3 variants for Reminder 1 AND 3 for Reminder 2. 10 = yes, 0 = wrong count.

Return ONLY valid JSON:
{
  "scores": {"r1_char_limit": N, "r1_binary_framing": N, "r1_no_repitch": N, "r2_char_limit": N, "r2_shelf_signal": N, "r2_open_door": N, "tone": N, "three_variants_each": N},
  "total": N,
  "max_total": 80,
  "failures": ["specific rule violations"],
  "suggestions": ["specific improvements to the skill prompt"]
}""",
}

# ── Programmatic pre-checks (gratis, geen API call nodig) ─────────────────────


def programmatic_checks(output: str, skill: str) -> list:
    """Snelle deterministische checks voordat de evaluator LLM wordt aangeroepen."""
    issues = []

    if skill == "first_message":
        # Tel varianten
        variants = re.findall(r"Option \d", output)
        if len(variants) < 3:
            issues.append(f"Verwacht 3 varianten, gevonden: {len(variants)}")

        # Check op verboden placeholders
        forbidden = re.findall(r"\[(?!Name\])[A-Za-z_]+\]", output)
        if forbidden:
            issues.append(f"Verboden placeholders: {set(forbidden)}")

        # Check op "if so" in plaats van "if not"
        if re.search(r"\bif so\b", output, re.I):
            issues.append("Gebruikt 'if so' in plaats van 'if not'")

        # Check op "ever" in berichten
        if re.search(r"\bever\b", output, re.I):
            issues.append("Gebruikt verboden woord 'ever'")

    elif skill == "reminders":
        # Check of er Reminder 1 en 2 secties zijn
        if not re.search(r"Reminder 1", output, re.I):
            issues.append("Reminder 1 sectie ontbreekt")
        if not re.search(r"Reminder 2", output, re.I):
            issues.append("Reminder 2 sectie ontbreekt")

    elif skill == "second_message":
        # Check split send markers
        if "SEND" not in output.upper():
            issues.append("LinkedIn split-send markers ontbreken")

        # Check of niet / or not aanwezig is
        if not re.search(r"of niet|or not", output, re.I):
            issues.append("'of niet'/'or not' ontbreekt in CTA")

    return issues


# ── Core functions ────────────────────────────────────────────────────────────


def generate_output(client: Anthropic, system_prompt: str, user_message: str) -> str:
    response = client.messages.create(
        model=GENERATOR_MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def evaluate_output(client: Anthropic, output: str, skill: str) -> dict:
    rubric = RUBRICS[skill]
    prompt = f"{rubric}\n\n--- OUTPUT TO EVALUATE ---\n\n{output}"

    response = client.messages.create(
        model=EVALUATOR_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text
    # Extract JSON from response (handle markdown code blocks)
    json_match = re.search(r"\{[\s\S]*\}", text)
    if json_match:
        return json.loads(json_match.group())
    raise ValueError(f"Evaluator returned non-JSON: {text[:200]}")


def improve_skill(client: Anthropic, skill_content: str, all_failures: list, all_suggestions: list) -> str:
    response = client.messages.create(
        model=EVALUATOR_MODEL,
        max_tokens=8192,
        messages=[{
            "role": "user",
            "content": (
                "Je bent een prompt engineer. Herschrijf dit skill-bestand om de gevonden "
                "fouten te verhelpen, terwijl je ALLE bestaande regels behoudt.\n\n"
                "HUIDIG SKILL BESTAND:\n"
                f"{skill_content}\n\n"
                "GEVONDEN FOUTEN:\n"
                f"{json.dumps(all_failures, indent=2, ensure_ascii=False)}\n\n"
                "SUGGESTIES:\n"
                f"{json.dumps(all_suggestions, indent=2, ensure_ascii=False)}\n\n"
                "Geef ALLEEN de verbeterde markdown content terug. Geen uitleg."
            ),
        }],
    )
    return response.content[0].text


def backup_skill(skill_file: str):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    src = os.path.join(BASE_DIR, skill_file)
    dst = os.path.join(BACKUP_DIR, os.path.basename(skill_file) + ".bak")
    shutil.copy2(src, dst)


def revert_skill(skill_file: str):
    src = os.path.join(BACKUP_DIR, os.path.basename(skill_file) + ".bak")
    dst = os.path.join(BASE_DIR, skill_file)
    shutil.copy2(src, dst)


def read_skill(skill_file: str) -> str:
    with open(os.path.join(BASE_DIR, skill_file), "r", encoding="utf-8") as f:
        return f.read()


def write_skill(skill_file: str, content: str):
    with open(os.path.join(BASE_DIR, skill_file), "w", encoding="utf-8") as f:
        f.write(content)


# ── Main loop ─────────────────────────────────────────────────────────────────


def run_tests(client: Anthropic, test_cases: list) -> list:
    system_prompt = build_system_prompt()
    results = []

    for tc in test_cases:
        print(f"  Genereren: {tc['id']}...", end=" ", flush=True)
        output = generate_output(client, system_prompt, tc["user_message"])

        # Programmatic pre-checks
        pre_issues = programmatic_checks(output, tc["skill"])

        # LLM evaluatie
        print("evalueren...", end=" ", flush=True)
        try:
            evaluation = evaluate_output(client, output, tc["skill"])
        except Exception as e:
            print(f"FOUT: {e}")
            evaluation = {"scores": {}, "total": 0, "max_total": 80, "failures": [str(e)], "suggestions": []}

        total = evaluation.get("total", 0)
        max_total = evaluation.get("max_total", 80)
        pct = (total / max_total * 100) if max_total > 0 else 0
        print(f"{total}/{max_total} ({pct:.0f}%)")

        results.append({
            "test_id": tc["id"],
            "skill": tc["skill"],
            "skill_file": tc["skill_file"],
            "output": output,
            "pre_check_issues": pre_issues,
            "evaluation": evaluation,
        })

    return results


def log_results(results: list, iteration: int):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(RESULTS_DIR, f"run_{timestamp}_iter{iteration}.json")

    totals = [r["evaluation"].get("total", 0) for r in results]
    max_totals = [r["evaluation"].get("max_total", 80) for r in results]
    avg = sum(totals) / len(totals) if totals else 0
    avg_max = sum(max_totals) / len(max_totals) if max_totals else 80

    data = {
        "timestamp": timestamp,
        "iteration": iteration,
        "aggregate_score": avg,
        "aggregate_max": avg_max,
        "aggregate_pct": (avg / avg_max * 100) if avg_max > 0 else 0,
        "results": results,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n  Resultaten opgeslagen: {filepath}")
    print(f"  Gemiddelde score: {avg:.1f}/{avg_max:.0f} ({data['aggregate_pct']:.0f}%)\n")
    return data


def print_summary(results: list):
    print("\n" + "=" * 60)
    print("SAMENVATTING")
    print("=" * 60)
    for r in results:
        ev = r["evaluation"]
        total = ev.get("total", 0)
        max_total = ev.get("max_total", 80)
        pct = (total / max_total * 100) if max_total > 0 else 0
        status = "OK" if pct >= 80 else "VERBETER" if pct >= 60 else "SLECHT"
        print(f"  [{status:>8}] {r['test_id']}: {total}/{max_total} ({pct:.0f}%)")

        if r["pre_check_issues"]:
            for issue in r["pre_check_issues"]:
                print(f"           Pre-check: {issue}")

        failures = ev.get("failures", [])
        if failures:
            for f in failures[:3]:
                print(f"           Fout: {f}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Optimaliseer More Deals outreach skills")
    parser.add_argument("--optimize", action="store_true", help="Skill bestanden verbeteren")
    parser.add_argument("--iterations", type=int, default=3, help="Max optimalisatie iteraties")
    parser.add_argument(
        "--skill",
        choices=["first_message", "second_message", "reminders"],
        help="Test alleen deze skill",
    )
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("FOUT: ANTHROPIC_API_KEY niet gevonden in .env")
        return

    client = Anthropic(api_key=api_key)

    # Filter test cases
    test_cases = TEST_CASES
    if args.skill:
        test_cases = [tc for tc in TEST_CASES if tc["skill"] == args.skill]
        print(f"Focus op skill: {args.skill} ({len(test_cases)} test cases)")

    # Baseline evaluatie
    print("\n--- BASELINE EVALUATIE ---\n")
    baseline_results = run_tests(client, test_cases)
    baseline_data = log_results(baseline_results, iteration=0)
    print_summary(baseline_results)

    if not args.optimize:
        print("\nKlaar. Gebruik --optimize om skills automatisch te verbeteren.")
        return

    # Optimalisatie loop
    best_score = baseline_data["aggregate_pct"]
    print(f"\n--- START OPTIMALISATIE ({args.iterations} iteraties) ---\n")

    for i in range(1, args.iterations + 1):
        print(f"=== Iteratie {i}/{args.iterations} ===\n")

        # Groepeer failures per skill file
        skill_failures = {}
        for r in baseline_results:
            sf = r["skill_file"]
            if sf not in skill_failures:
                skill_failures[sf] = {"failures": [], "suggestions": []}
            skill_failures[sf]["failures"].extend(r["evaluation"].get("failures", []))
            skill_failures[sf]["suggestions"].extend(r["evaluation"].get("suggestions", []))

        # Verbeter elke skill file die problemen heeft
        modified_skills = []
        for skill_file, data in skill_failures.items():
            if not data["failures"]:
                print(f"  {skill_file}: geen fouten, overslaan")
                continue

            print(f"  {skill_file}: {len(data['failures'])} fouten, verbeteren...")
            backup_skill(skill_file)
            current_content = read_skill(skill_file)
            improved = improve_skill(client, current_content, data["failures"], data["suggestions"])
            write_skill(skill_file, improved)
            modified_skills.append(skill_file)

        if not modified_skills:
            print("\nGeen skills om te verbeteren. Stoppen.")
            break

        # Hertest
        print(f"\n  Hertesten na verbetering...\n")
        new_results = run_tests(client, test_cases)
        new_data = log_results(new_results, iteration=i)
        print_summary(new_results)

        new_score = new_data["aggregate_pct"]

        # Vergelijk en houd/verwerp
        if new_score >= best_score:
            print(f"  BEHOUDEN: score {best_score:.0f}% -> {new_score:.0f}%")
            best_score = new_score
            baseline_results = new_results
        else:
            print(f"  TERUGGEDRAAID: score daalde {best_score:.0f}% -> {new_score:.0f}%")
            for skill_file in modified_skills:
                revert_skill(skill_file)

    print(f"\n--- OPTIMALISATIE KLAAR ---")
    print(f"Beste score: {best_score:.0f}%\n")


if __name__ == "__main__":
    main()
