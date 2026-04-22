# Model: gpt-4o-mini (tool-calling agent, reasoning done by algorithms)
"""
RegressionTestAgent — runs the Maven test suite after fixes are applied
and reports whether the fix resolved the issue without breaking anything else.
"""
import os, json, subprocess, re
from llm_client import get_client, get_model
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, '.env'))
client = get_client()

JAVA_DIR = os.path.join(ROOT, 'java-order-service')
MVN_CANDIDATES = [
    "/sessions/tender-loving-maxwell/apache-maven-3.9.6/bin/mvn",
    "mvn",
    "/usr/local/bin/mvn",
    "/usr/bin/mvn"
]

def find_mvn():
    for path in MVN_CANDIDATES:
        if os.path.exists(path):
            return path
        result = subprocess.run(["which", path], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    return "mvn"

def run_tests(test_class: str = None) -> dict:
    """
    Run the Maven test suite. Returns structured results.
    """
    mvn = find_mvn()
    cmd = [mvn, "test", "--no-transfer-progress"]
    if test_class:
        cmd += [f"-Dtest={test_class}"]

    try:
        result = subprocess.run(
            cmd, cwd=JAVA_DIR,
            capture_output=True, text=True, timeout=120
        )
        output = result.stdout + result.stderr
        return _parse_maven_output(output, result.returncode)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": "Maven test timed out after 120s"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def _parse_maven_output(output: str, returncode: int) -> dict:
    """Parse Maven output into structured results."""
    tests_run = failures = errors = skipped = 0

    match = re.search(r'Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)', output)
    if match:
        tests_run, failures, errors, skipped = [int(x) for x in match.groups()]

    # Find failed test names
    failed_tests = re.findall(r'FAILED\s+([\w.]+)', output)

    build_success = "BUILD SUCCESS" in output

    return {
        "status": "pass" if build_success else "fail",
        "build_success": build_success,
        "tests_run": tests_run,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "passed": tests_run - failures - errors,
        "failed_tests": failed_tests,
        "raw_output_tail": output[-1500:] if len(output) > 1500 else output
    }

def interpret_results(test_results: dict, fixes_applied: list) -> dict:
    """
    Use GPT-4o-mini to interpret test results in context of the fixes applied.
    """
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": """You are RegressionTestAgent.
Given Maven test results and the list of fixes applied, determine:
1. Did the fixes resolve the issue?
2. Did any fix introduce a regression?
3. Are there remaining failures unrelated to the fixes?

Return JSON:
{
  "verdict": "all_fixed|partial_fix|regression_introduced|unrelated_failures",
  "fix_effectiveness": [{"fix": str, "resolved": bool, "evidence": str}],
  "regressions": [str],
  "remaining_issues": [str],
  "recommendation": str,
  "ready_for_production": bool
}"""},
            {"role": "user", "content": json.dumps({
                "test_results": test_results,
                "fixes_applied": fixes_applied
            }, indent=2)}
        ],
        response_format={"type": "json_object"},
        temperature=0,
        timeout=60
    )
    return json.loads(response.choices[0].message.content)

def verify_fix(fixes_applied: list = None) -> dict:
    """Full verification flow: run tests then interpret results."""
    print("  Running Maven test suite...")
    test_results = run_tests()

    status_icon = "✅" if test_results.get("build_success") else "❌"
    print(f"  {status_icon} Tests: {test_results.get('passed',0)} passed, "
          f"{test_results.get('failures',0)} failed, "
          f"{test_results.get('errors',0)} errors")

    if fixes_applied:
        interpretation = interpret_results(test_results, fixes_applied)
    else:
        interpretation = None

    return {
        "test_results": test_results,
        "interpretation": interpretation
    }


if __name__ == "__main__":
    sample_fixes = [
        {"fix_description": "Changed 'qty' to 'quantity' in Python inventory service"},
        {"fix_description": "Removed int cast from SqliteOrderRepository total field"}
    ]
    result = verify_fix(sample_fixes)
    print(json.dumps(result, indent=2))
