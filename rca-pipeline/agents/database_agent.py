# Model: gpt-4o-mini (tool-calling agent, reasoning done by algorithms)
"""
DatabaseAgent — REAL tool-using agent (ReAct loop via OpenAI tool_use API).

Inspects SQLite database schema, data integrity, and type mismatches
using real database queries.

Tools available:
  - list_tables()                        → SELECT name FROM sqlite_master
  - describe_table(table_name)           → PRAGMA table_info
  - run_query(sql)                       → execute SELECT query (limited to 50 rows)
  - count_rows(table_name)               → SELECT COUNT(*)
  - find_anomalous_values(table_name, column_name) → statistical outliers
  - check_recent_rows(table_name, n)    → SELECT ... ORDER BY rowid DESC
  - detect_type_mismatches(table_name)  → sample rows and check declared vs stored types
  - finish_analysis(schema_issues, data_anomalies, suspicious_records, recommendations, summary)

Real SQLite3 queries. Falls back to text parsing if DB file unavailable.
"""
import os, sys, json, sqlite3, re, statistics
from llm_client import get_client, get_model
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, '.env'))
client = get_client()

# Try to find the database file
DB_PATHS = [
    os.path.join(ROOT, "order-db.sqlite"),
    os.path.join(ROOT, "orders.db"),
    os.path.join(ROOT, "order.db"),
]

_db_connection = None
_db_path = None

def _get_db_connection(db_path: str = None):
    """Get or create a database connection."""
    global _db_connection, _db_path

    if db_path is None:
        # Auto-detect from standard paths
        for path in DB_PATHS:
            if os.path.exists(path):
                db_path = path
                break

    if db_path is None:
        return None, None

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _db_path = db_path
        return conn, db_path
    except Exception:
        return None, None

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions (OpenAI function-calling schema)
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "Get list of all tables in the database.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": "Get schema of a table: column names, types, constraints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "Name of the table"},
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_query",
            "description": "Execute a SELECT query (read-only). Returns up to 50 rows.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL SELECT query"},
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_rows",
            "description": "Count total rows in a table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_anomalous_values",
            "description": "Find statistical outliers in a numeric column: values outside mean ± 3*std. Also counts NULLs and distinct values.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name":  {"type": "string"},
                    "column_name": {"type": "string", "description": "Numeric column to analyze"},
                },
                "required": ["table_name", "column_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_recent_rows",
            "description": "Get the most recent N rows from a table (ordered by rowid DESC).",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                    "n":          {"type": "integer", "description": "Number of rows to fetch (default 10)", "default": 10},
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_type_mismatches",
            "description": "Sample rows from a table and check if stored values match declared column types. Returns type mismatch evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                },
                "required": ["table_name"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are DatabaseAgent, an AI agent in an incident Root Cause Analysis pipeline.

You have tools to inspect a SQLite database: schema, data types, values, and anomalies.

Your investigation strategy:
1. First, list all tables and get a high-level schema picture.
2. For each table, describe the schema to understand column types.
3. Check recent rows to see what actual data looks like.
4. Use detect_type_mismatches to find where declared type ≠ stored type.
5. Use find_anomalous_values on numeric columns to spot truncation or overflow.
6. For suspicious tables, run custom SELECT queries to dig deeper.
7. Look for: type mismatches (REAL vs INT stored), missing constraints, data truncation.
8. Identify which tables have issues caused by the cross-service bugs.

When done, call finish_analysis with schema issues, data anomalies, and recommendations.
"""

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit your final database analysis.",
        "parameters": {
            "type": "object",
            "properties": {
                "schema_issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "table":        {"type": "string"},
                            "column":       {"type": "string"},
                            "issue_type":   {"type": "string"},
                            "severity":     {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]},
                            "description":  {"type": "string"},
                            "evidence":     {"type": "string"},
                            "suggested_fix": {"type": "string"},
                        },
                    },
                },
                "data_anomalies": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "table":        {"type": "string"},
                            "description":  {"type": "string"},
                            "affected_rows": {"type": "integer"},
                            "example":      {"type": "string"},
                        },
                    },
                },
                "suspicious_records": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "table":       {"type": "string"},
                            "record_id":   {"type": "string"},
                            "issue":       {"type": "string"},
                            "value":       {"type": "string"},
                        },
                    },
                },
                "recommendations": {"type": "array", "items": {"type": "string"}},
                "summary":         {"type": "string"},
            },
            "required": ["summary"],
        },
    },
}

ALL_TOOLS = TOOLS + [FINISH_TOOL]

# ─────────────────────────────────────────────────────────────────────────────
# Tool executor — real SQLite queries
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool call and return the result as a string."""

    if name == "list_tables":
        conn, _ = _get_db_connection()
        if conn is None:
            return json.dumps({"error": "No database connection"})

        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cur.fetchall()]
            cur.close()
            return json.dumps({"tables": tables})
        except Exception:
            return json.dumps({"error": "Operation failed. Check logs for details."})
        finally:
            conn.close()

    elif name == "describe_table":
        table_name = args.get("table_name", "")
        conn, _ = _get_db_connection()
        if conn is None:
            return json.dumps({"error": "No database connection"})

        try:
            cur = conn.cursor()
            cur.execute(f"PRAGMA table_info({table_name})")
            rows = cur.fetchall()
            cur.close()

            columns = []
            for row in rows:
                columns.append({
                    "cid":       row[0],
                    "name":      row[1],
                    "type":      row[2],
                    "notnull":   bool(row[3]),
                    "dflt_value": row[4],
                    "pk":        bool(row[5])
                })

            return json.dumps({"table": table_name, "columns": columns})
        except Exception:
            return json.dumps({"error": "Operation failed. Check logs for details."})
        finally:
            conn.close()

    elif name == "run_query":
        sql = args.get("sql", "")
        conn, _ = _get_db_connection()
        if conn is None:
            return json.dumps({"error": "No database connection"})

        # Guard: only SELECT
        if not sql.strip().upper().startswith("SELECT"):
            return json.dumps({"error": "Only SELECT queries allowed"})

        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cur.close()

            # Convert to dicts
            results = []
            for row in rows[:50]:
                results.append(dict(row))

            return json.dumps({"query": sql, "rows": results, "total": len(rows)})
        except Exception:
            return json.dumps({"error": "Operation failed. Check logs for details."})
        finally:
            conn.close()

    elif name == "count_rows":
        table_name = args.get("table_name", "")
        conn, _ = _get_db_connection()
        if conn is None:
            return json.dumps({"error": "No database connection"})

        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {table_name}")
            count = cur.fetchone()[0]
            cur.close()
            return json.dumps({"table": table_name, "row_count": count})
        except Exception:
            return json.dumps({"error": "Operation failed. Check logs for details."})
        finally:
            conn.close()

    elif name == "find_anomalous_values":
        table_name = args.get("table_name", "")
        column_name = args.get("column_name", "")
        conn, _ = _get_db_connection()
        if conn is None:
            return json.dumps({"error": "No database connection"})

        try:
            cur = conn.cursor()

            # Get all numeric values
            cur.execute(f"SELECT {column_name} FROM {table_name} WHERE {column_name} IS NOT NULL")
            values = [row[0] for row in cur.fetchall()]

            if not values:
                cur.close()
                return json.dumps({
                    "table": table_name,
                    "column": column_name,
                    "error": "No non-NULL values found"
                })

            # Convert to float
            try:
                numeric_values = [float(v) for v in values]
            except (ValueError, TypeError):
                cur.close()
                return json.dumps({
                    "table": table_name,
                    "column": column_name,
                    "error": "Column is not numeric"
                })

            # Statistics
            mean = statistics.mean(numeric_values)
            stdev = statistics.stdev(numeric_values) if len(numeric_values) > 1 else 0.0
            threshold = 3 * stdev

            # Find outliers
            outliers = [v for v in numeric_values if abs(v - mean) > threshold]

            # Count nulls
            cur.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {column_name} IS NULL")
            null_count = cur.fetchone()[0]

            # Count distinct
            cur.execute(f"SELECT COUNT(DISTINCT {column_name}) FROM {table_name}")
            distinct_count = cur.fetchone()[0]

            cur.close()

            return json.dumps({
                "table":         table_name,
                "column":        column_name,
                "count":         len(numeric_values),
                "mean":          round(mean, 4),
                "stdev":         round(stdev, 4),
                "min":           round(min(numeric_values), 4),
                "max":           round(max(numeric_values), 4),
                "outliers":      [round(v, 4) for v in outliers[:10]],
                "outlier_count": len(outliers),
                "null_count":    null_count,
                "distinct_count": distinct_count,
            })
        except Exception:
            return json.dumps({"error": "Operation failed. Check logs for details."})
        finally:
            conn.close()

    elif name == "check_recent_rows":
        table_name = args.get("table_name", "")
        n = int(args.get("n", 10))
        conn, _ = _get_db_connection()
        if conn is None:
            return json.dumps({"error": "No database connection"})

        try:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM {table_name} ORDER BY rowid DESC LIMIT {n}")
            rows = cur.fetchall()
            cur.close()

            results = []
            for row in rows:
                results.append(dict(row))

            return json.dumps({"table": table_name, "rows": results})
        except Exception:
            return json.dumps({"error": "Operation failed. Check logs for details."})
        finally:
            conn.close()

    elif name == "detect_type_mismatches":
        table_name = args.get("table_name", "")
        conn, _ = _get_db_connection()
        if conn is None:
            return json.dumps({"error": "No database connection"})

        try:
            cur = conn.cursor()

            # Get schema
            cur.execute(f"PRAGMA table_info({table_name})")
            columns = [(row[1], row[2]) for row in cur.fetchall()]

            # Get sample rows
            cur.execute(f"SELECT * FROM {table_name} LIMIT 20")
            sample_rows = cur.fetchall()

            mismatches = []

            for col_name, col_type in columns:
                col_type_upper = col_type.upper()

                for row in sample_rows:
                    row_dict = dict(row)
                    value = row_dict.get(col_name)

                    if value is None:
                        continue

                    # Check type mismatch
                    is_mismatch = False
                    issue = None

                    if "INT" in col_type_upper:
                        if not isinstance(value, int):
                            is_mismatch = True
                            issue = f"Declared INT but stored {type(value).__name__}: {value}"
                    elif "REAL" in col_type_upper or "FLOAT" in col_type_upper:
                        # Real should store floats, but SQLite can store ints
                        # Check if it looks truncated (whole number when should be decimal)
                        if isinstance(value, (int, float)):
                            if isinstance(value, int) and "REAL" in col_type_upper:
                                # Might be truncation
                                issue = f"Declared REAL but stored integer: {value} (possible truncation)"
                    elif "TEXT" in col_type_upper or "CHAR" in col_type_upper:
                        if not isinstance(value, str):
                            is_mismatch = True
                            issue = f"Declared TEXT but stored {type(value).__name__}: {value}"

                    if issue:
                        mismatches.append({
                            "column":   col_name,
                            "declared": col_type,
                            "actual":   type(value).__name__,
                            "value":    str(value)[:100],
                            "issue":    issue
                        })

            cur.close()
            return json.dumps({
                "table": table_name,
                "mismatches": mismatches,
                "total_mismatches": len(mismatches)
            })

        except Exception:
            return json.dumps({"error": "Operation failed. Check logs for details."})
        finally:
            conn.close()

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def analyze_database(db_context: str, db_path: str = None) -> dict:
    """Real tool-using ReAct agent for database analysis.

    The LLM calls SQLite query tools to inspect database schema and data.

    Args:
        db_context: Database schema or context information as a string.
        db_path: Optional path to the SQLite database file.

    Returns:
        dict: Analysis results with schema_issues, data_anomalies, suspicious_records, and recommendations.

    Max iterations: 8.
    """

    # Initialize DB connection
    conn, found_path = _get_db_connection(db_path)
    if conn is None and db_path is None and db_context:
        # Fall back to text parsing if no DB file
        pass  # Will use db_context in the prompt

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content":
         f"Investigate the database for schema issues and data anomalies.\n\n"
         f"Database context (or schema if live DB unavailable):\n{db_context[:2000]}\n\n"
         f"Use your tools to inspect the actual database. Check for type mismatches, "
         f"anomalous values, and data integrity issues. When done, call finish_analysis()."},
    ]

    final_result = {}
    max_iterations = 8

    for iteration in range(max_iterations):
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=ALL_TOOLS,
            tool_choice="auto",
            temperature=0,
            timeout=60,
        )

        msg = response.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            break

        all_done = False
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments or "{}")

            if fn_name == "finish_analysis":
                final_result = fn_args
                all_done = True
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      json.dumps({"status": "accepted"}),
                })
            else:
                result = _execute_tool(fn_name, fn_args)
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result,
                })

        if all_done:
            break

    # Fallback
    if not final_result:
        final_result = {
            "schema_issues":     [],
            "data_anomalies":    [],
            "suspicious_records": [],
            "summary": "Database analysis incomplete (max iterations reached)"
        }

    final_result.setdefault("schema_issues", [])
    final_result.setdefault("data_anomalies", [])
    final_result.setdefault("suspicious_records", [])
    final_result.setdefault("recommendations", [])
    final_result.setdefault("summary", "Database analysis completed")

    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample_db_context = """
    Table: orders
    Columns:
      id TEXT PRIMARY KEY
      customer_id TEXT NOT NULL
      total REAL         ← declared REAL
      status TEXT
      created_at TEXT

    Sample rows (recent):
      ('ORD-8820', 'CUST-001', 99,    'confirmed', '2024-01-15 10:22:01')
      ('ORD-8819', 'CUST-002', 149,   'confirmed', '2024-01-15 10:21:44')
    """

    result = analyze_database(sample_db_context)
    print(json.dumps(result, indent=2))
