import os
from google import genai
from google.genai import types
from tools import get_business_dates, search_inspections

# Initialize GenAI Client using Vertex AI
client = genai.Client(
    vertexai=True,
    project=os.getenv("GOOGLE_CLOUD_PROJECT"),
    location=os.getenv("GOOGLE_CLOUD_LOCATION"),
)

# Register local Python functions as executable tools
tool_list = [
    get_business_dates,
    search_inspections,
]

# Create a Chat session with AFC (Automatic Function Calling)
chat = client.chats.create(
    model="gemini-2.5-flash",
    config=types.GenerateContentConfig(
        tools=tool_list,
        temperature=0.2,
    )
)

prompt = """
Run the Dallas morning CSR checklist checks for today. 
1. Call `get_business_dates` first to get today's date and the previous business day.
2. Query unassigned inspections for division 'Dallas' for today.
3. Query prior day incomplete items ('Assigned' status) for division 'Dallas' for the previous business day.
4. Query cancellations and not-ready records ('Cancelled in' and 'Not Ready' resolutions) for division 'Dallas' for today.

Summarize all findings clearly in an operational report.
"""

print("🚀 Executing Dallas CSR Agent Test...\n")

response = chat.send_message(prompt)

print(response.text)