"""
JavaFixAgent — generates and applies code fixes to Java source files.
Works the same as FixGeneratorAgent but handles the Java service specifically:
  - loads all Java source files from java-order-service/
  - generates find/replace patches via GPT-4o
  - applies patches via Python string replacement
  - verifies each fix compiles with `mvn compile`
  - rolls back automatically if compile fails
"""
import os, json, glob, subprocess, shutil, logging
from llm_client import get_client, get_model
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA_DIR  = os.path.join(ROOT, "java-order-service")
JAVA_SRC  = os.path.join(JAVA_DIR, "src", "main", "java", "com", "aspire")
load_dotenv(os.path.join(ROOT, '.env'))
client = get_client()

MVN_CANDIDATES = [
    "/sessions/tender-loving-maxwell/apache-maven-3.9.6/bin/mvn",
    "mvn", "/usr/local/bin/mvn", "/usr/bin/mvn"
]

def find_mvn():
    for p in MVN_CANDIDATES:
        if os.path.isfile(p): return p
    return "mvn"

JAVA_FIX_PROMPT = """You are JavaFixAgent, part of an AI-powered Root Cause Analysis pipeline.

You receive a root cause hypothesis and a set of Java source files.
Your job is to generate minimal, precise, compile-safe fixes.

Rules:
- Produce ONE fix object per file that needs changing
- The 'find' string MUST be unique in its file (include enough context lines)
- The 'replace' string must be syntactically valid Java
- Do NOT change method signatures, imports, or class structure unless required
- Do NOT change comments or whitespace outside the bug area
- Prefer changing the sending side (Java) to match the receiving side's expectation,
  unless the receiving side is more wrong

Return JSON:
{
  "fixes": [
    {
      "fix_id": str,
      "file": str (relative to java-order-service/src/main/java/com/aspire/),
      "fix_description": str,
      "find": str (exact multi-line string to find — must be unique in file),
      "replace": str (exact replacement — syntactically valid Java),
      "confidence": float (0-1),
      "breaking_risk": "none|low|medium|high",
      "explanation": str
    }
  ],
  "hypothesis_addressed": str,
  "estimated_risk": "safe|low_risk|medium_risk|high_risk"
}
"""

def load_java_files() -> dict:
    """Load all Java source files from the order service."""
    files = {}
    for path in glob.glob(os.path.join(JAVA_SRC, "**", "*.java"), recursive=True):
        rel = os.path.relpath(path, os.path.join(JAVA_DIR, "src", "main", "java", "com", "aspire"))
        with open(path) as f:
            files[rel] = f.read()
    return files

def generate_java_fixes(hypothesis: dict, java_files: dict = None) -> dict:
    """Use GPT-4o to generate fixes for the given hypothesis across Java files."""
    if java_files is None:
        java_files = load_java_files()

    code_block = ""
    for rel_path, src in java_files.items():
        code_block += f"\n\n=== {rel_path} ===\n{src}"

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": JAVA_FIX_PROMPT},
            {"role": "user",   "content":
                f"Hypothesis:\n{json.dumps(hypothesis, indent=2)}\n\n"
                f"Java source files:\n{code_block}"}
        ],
        response_format={"type": "json_object"},
        temperature=0,
        timeout=60
    )
    try:
        return json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, AttributeError):
        return {"error": "Failed to parse LLM response", "fixes": []}

def _full_java_path(rel_path: str) -> str:
    """Convert relative path (e.g. 'inventory/HttpInventoryClient.java') to absolute."""
    return os.path.join(JAVA_SRC, rel_path)

def _mvn_compile() -> tuple[bool, str]:
    """Run mvn compile. Returns (success, output)."""
    mvn = find_mvn()
    result = subprocess.run(
        [mvn, "compile", "--no-transfer-progress", "-q"],
        cwd=JAVA_DIR, capture_output=True, text=True, timeout=60
    )
    output = result.stdout + result.stderr
    return result.returncode == 0, output

def apply_java_fixes(fix_result: dict, verify_compile: bool = True) -> list[dict]:
    """
    Apply all fixes from generate_java_fixes() output.
    Verifies each fix compiles; rolls back if not.
    Returns list of applied fix records with status.
    """
    applied = []
    fixes = fix_result.get("fixes", [])

    for fix in fixes:
        rel_path = fix.get("file", "")
        full_path = _full_java_path(rel_path)

        if not os.path.exists(full_path):
            applied.append({**fix, "status": "error", "error": f"File not found: {full_path}"})
            logger.error(f"  ❌  File not found: {rel_path}")
            continue

        with open(full_path) as f:
            original = f.read()

        find_str    = fix.get("find", "")
        replace_str = fix.get("replace", "")

        if not find_str or find_str not in original:
            applied.append({**fix, "status": "not_found",
                           "error": f"Pattern not found in {rel_path}"})
            logger.info(f"  ⚠️   Pattern not found in {rel_path} — skipping")
            continue

        # Apply patch
        patched = original.replace(find_str, replace_str, 1)
        with open(full_path, "w") as f:
            f.write(patched)

        logger.info(f"  📝  Patched {rel_path}")

        if verify_compile:
            success, output = _mvn_compile()
            if success:
                applied.append({**fix, "status": "applied_and_verified"})
                logger.info(f"  ✅  {fix.get('fix_description','')}")
                logger.info(f"      Compiled successfully ✓")
            else:
                # Roll back
                with open(full_path, "w") as f:
                    f.write(original)
                applied.append({**fix, "status": "rolled_back",
                                "compile_error": output[-500:]})
                logger.error(f"  ❌  Compile failed — rolled back {rel_path}")
                logger.error(f"      {output[-300:]}")
        else:
            applied.append({**fix, "status": "applied"})
            logger.info(f"  ✅  {fix.get('fix_description','')} (compile not verified)")

    return applied

def fix_java_hypothesis(hypothesis: dict, verify_compile: bool = True) -> dict:
    """
    Full flow: generate fixes for a hypothesis → apply → verify.
    Returns dict with generated fixes and applied results.
    """
    logger.info(f"  🔍  Generating Java fixes for: {hypothesis.get('hypothesis','')[:60]}")
    java_files = load_java_files()
    fix_result = generate_java_fixes(hypothesis, java_files)

    n_fixes = len(fix_result.get("fixes", []))
    risk = fix_result.get("estimated_risk", "?")
    logger.info(f"  💡  {n_fixes} fix(es) generated | Risk: {risk}")

    applied = apply_java_fixes(fix_result, verify_compile=verify_compile)
    return {"generated": fix_result, "applied": applied}


if __name__ == "__main__":
    # Smoke test: generate (but don't apply) fixes for the known bugs
    hyp = {
        "rank": 1,
        "hypothesis": "Java sends 'qty' but Python inventory service expects 'quantity'",
        "confidence": 0.97,
        "fix_category": "field_rename",
        "affected_services": ["java-order-service", "python-inventory-service"]
    }
    java_files = load_java_files()
    logger.info(f"Loaded {len(java_files)} Java files")
    result = generate_java_fixes(hyp, java_files)
    logger.info(json.dumps(result, indent=2))
