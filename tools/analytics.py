import os
from pathlib import Path

# Force OpenSSL legacy config BEFORE loading database drivers
cnf_path = Path(__file__).resolve().parent.parent / "openssl_legacy.cnf"
if cnf_path.exists():
    os.environ["OPENSSL_CONF"] = str(cnf_path)

import re
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for server generation
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
from sqlalchemy import create_engine, URL
from dotenv import load_dotenv

load_dotenv(override=True)

# Build database engine from environment configuration
connection_url = URL.create(
    "mssql+pymssql",
    username=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    host=os.getenv("DB_SERVER"),
    database=os.getenv("DB_NAME"),
)
engine = create_engine(connection_url)

# Ensure chart output directory exists
CHARTS_DIR = os.path.join(os.getcwd(), "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)


def execute_sql_query(sql_query: str) -> list[dict]:
    """
    Executes a read-only SQL SELECT query against the ai.VW_Inspection and ai.VW_InspectionDefects views.
    
    Args:
        sql_query (str): A valid T-SQL query string. Must be a SELECT query.
        
    Returns:
        list[dict]: List of record dictionaries representing query results.
    """
    # Guardrail: Enforce read-only SELECT queries
    clean_query = sql_query.strip().strip(";").lower()
    if not clean_query.startswith("select") and not clean_query.startswith("with"):
        return [{"error": "Security Error: Only SELECT queries are permitted."}]

    try:
        df = pd.read_sql(sql_query, con=engine)
        # Limit rows returned to LLM context to prevent token overflows
        if len(df) > 100:
            return {
                "total_rows": len(df),
                "data": df.head(100).to_dict(orient="records"),
                "note": "Output truncated to top 100 rows for display."
            }
        return df.to_dict(orient="records")
    except Exception as err:
        return [{"error": f"Database Execution Error: {str(err)}"}]


def generate_chart(
    sql_query: str, 
    chart_type: str, 
    x_col: str, 
    y_col: str, 
    title: str, 
    hue_col: str = None
) -> dict:
    """
    Executes a SQL query, renders a chart using Matplotlib/Seaborn, and saves it to disk.

    Args:
        sql_query (str): T-SQL query supplying data for the visualization.
        chart_type (str): Type of chart: 'bar', 'line', 'pie', or 'scatter'.
        x_col (str): DataFrame column name for the X-axis.
        y_col (str): DataFrame column name for the Y-axis.
        title (str): Descriptive title for the chart.
        hue_col (str, optional): Column name for sub-grouping/color differentiation.

    Returns:
        dict: Path to the generated image file and data summary.
    """
    try:
        df = pd.read_sql(sql_query, con=engine)
        if df.empty:
            return {"error": "No data returned for chart generation."}

        # Apply executive design styling
        sns.set_theme(style="whitegrid")
        plt.figure(figsize=(10, 6))

        if chart_type == "bar":
            sns.barplot(data=df, x=x_col, y=y_col, hue=hue_col, palette="viridis")
            plt.xticks(rotation=45, ha="right")
        elif chart_type == "line":
            sns.lineplot(data=df, x=x_col, y=y_col, hue=hue_col, marker="o", linewidth=2.5)
            plt.xticks(rotation=45, ha="right")
        elif chart_type == "pie":
            plt.pie(df[y_col], labels=df[x_col], autopct="%1.1f%%", startangle=140)
        else:
            sns.scatterplot(data=df, x=x_col, y=y_col, hue=hue_col, s=100)

        plt.title(title, fontsize=14, fontweight="bold", pad=15)
        plt.tight_layout()

        # Save chart file
        filename = re.sub(r"[^\w\-]", "_", title.lower()) + ".png"
        filepath = os.path.join(CHARTS_DIR, filename)
        plt.savefig(filepath, dpi=300)
        plt.close()

        return {
            "status": "success",
            "chart_path": filepath,
            "data_summary": df.head(10).to_dict(orient="records")
        }
    except Exception as err:
        return {"error": f"Chart Generation Error: {str(err)}"}