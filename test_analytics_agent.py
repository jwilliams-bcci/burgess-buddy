import os
from pathlib import Path

# Enforce OpenSSL workaround for macOS
cnf_path = Path(__file__).parent / "openssl_legacy.cnf"
if cnf_path.exists():
    os.environ["OPENSSL_CONF"] = str(cnf_path)

from google import genai
from google.genai import types
from tools.analytics import execute_sql_query, generate_chart

# Map tool names to Python functions
TOOL_MAP = {
    "execute_sql_query": execute_sql_query,
    "generate_chart": generate_chart,
}

SYSTEM_INSTRUCTION = """
You are the Senior Data Analyst AI for Burgess Construction Consultants.
Your goal is to answer executive questions accurately by querying database views and generating visual charts.

DATABASE SCHEMA DEFINITIONS:

View 1: `ai.VW_Inspection` (Inspection Header Level)
- `InspectionID` (INT): Unique ID
- `InspectionNumber` (INT), `InspectionDate` (DATETIME): Primary date filter
- `DivisionName` (VARCHAR): e.g., 'Dallas', 'Raleigh'
- `BuilderName` (VARCHAR), `Community` (VARCHAR), `City` (VARCHAR)
- `InspectorID` (INT), `InspectorName` (VARCHAR): Assigned consultant/inspector
- `InspectionType` (VARCHAR): Name of inspection type
- `InspectionClass` (INT): 7 = Energy Inspection
- `Resolution` (VARCHAR): Passed, Failed, Cancelled, Not Ready, etc.
- `InspectionStartTime` (DATETIME), `InspectionSubmitTime` (DATETIME)
- `ExpectedDuration` (INT): Target duration in minutes
- `TotalNumberOfDefects` (INT), `NumberOfDefectsRepaired` (INT)
- `Fee` (DECIMAL), `IsReinspect` (BIT), `IsInvoiced` (BIT)

View 2: `ai.VW_InspectionDefects` (Defect Line Items)
- `InspectionDetailID` (INT), `InspectionID` (INT)
- `CreateDate` (DATETIME), `LotNumber` (VARCHAR), `StageOfConstruction` (VARCHAR)
- `DefectCategory` (VARCHAR): High-level category
- `DefectItemNumber` (VARCHAR), `DefectDescription` (VARCHAR)
- `DefectStatus` (VARCHAR): Status of defect

BUSINESS RULES FOR QUERY GENERATION:
1. "1st time" inspections: Filter by `IsReinspect = 0` or `IsReinspect = 'False'`.
2. "Predrywall / like named" inspections: Use `InspectionType LIKE '%predrywall%'` or `%pre-drywall%` to capture all variations.
3. Timeframes: Always filter using `InspectionDate` (e.g., `InspectionDate >= DATEADD(month, -3, GETDATE())`). Default to 3 months or YTD if no timeframe is specified.
4. Average Per Day: Calculate daily averages by dividing total qualifying inspections by the distinct count of active inspection dates (`COUNT(DISTINCT CAST(InspectionDate AS DATE))`).
5. Energy Inspections: Filter by `InspectionClass = 7`.
6. "Energy-Only" Consultants: Consultants where `COUNT(CASE WHEN InspectionClass <> 7 THEN 1 END) = 0`.
7. Active Consultants: Unless specified otherwise, define active consultants as those with at least 1 inspection in the last 90 days (`InspectionDate >= DATEADD(month, -3, GETDATE())`).
8. Productivity Metrics: 
   - "Revenue" is SUM(ISNULL(Fee, 0)).
   - "Employees" / "Consultants" refers to COUNT(DISTINCT InspectorID).
   - "Revenue per Inspection" = SUM(Fee) / COUNT(InspectionID).
   - "Inspections per Employee" = COUNT(InspectionID) / COUNT(DISTINCT InspectorID).
   - "Revenue per Employee" = SUM(Fee) / COUNT(DISTINCT InspectorID).
9. Terminology & Synonyms: Terms like "Market", "Branch", or "Region" map directly to `DivisionName`.
10. Test/Training Data Exclusion: Always exclude test or training records by including `ISNULL(BuilderName, '') NOT LIKE 'ABC%'` in the WHERE clause.
11. Dialect Compatibility: SQL Server version is 2016 or earlier. Do NOT use `STRING_AGG()`.
12. Benchmark Comparisons: Calculate company benchmarks by averaging the calculated division metrics (e.g., `AVG(RevPerEmployee)` from a Division level CTE) rather than aggregating company-wide raw totals, to avoid cross-division inspector duplication skewing averages.
"""

client = genai.Client(
    vertexai=True,
    project=os.getenv("GOOGLE_CLOUD_PROJECT"),
    location=os.getenv("GOOGLE_CLOUD_LOCATION"),
)


def ask_exec_agent(prompt: str):
    print(f"👤 Executive Question: {prompt}\n")

    chat = client.chats.create(
        model="gemini-2.5-flash",
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=[execute_sql_query, generate_chart],
            temperature=0.1,
        ),
    )

    response = chat.send_message(prompt)

    # Agent tool execution loop
    while response.function_calls:
        for call in response.function_calls:
            fn_name = call.name
            fn_args = call.args
            print(f"⚙️ Agent executing tool: {fn_name}")
            if fn_name == "execute_sql_query":
                print(f"   SQL Query:\n   {fn_args.get('sql_query')}\n")

            if fn_name in TOOL_MAP:
                tool_output = TOOL_MAP[fn_name](**fn_args)

                # Send tool response back to Gemini
                response = chat.send_message(
                    types.Part.from_function_response(
                        name=fn_name, response={"result": tool_output}
                    )
                )
            else:
                print(f"❌ Tool {fn_name} not found in TOOL_MAP.")
                break

    print(f"🤖 Agent Response:\n{response.text}\n")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    exec_question = (
        "I'd like to see all scopes of work added to the database within the last 3 years. Please include the dates the scopes were added and the dates the builders were added."
    )
    ask_exec_agent(exec_question)