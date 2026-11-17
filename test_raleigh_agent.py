import os
from google import genai
from google.genai import types
from tools import (
    get_business_dates,
    get_unassigned_inspections,
    get_prior_day_assigned,
    get_cancellations_and_not_ready,
)

# Initialize GenAI Client using Vertex AI settings
client = genai.Client(
    vertexai=True,
    project=os.getenv("GOOGLE_CLOUD_PROJECT"),
    location=os.getenv("GOOGLE_CLOUD_LOCATION"),
)

# Register local Python functions as executable agent tools
tool_list = [
    get_business_dates,
    get_unassigned_inspections,
    get_prior_day_assigned,
    get_cancellations_and_not_ready,
]

# Create a Chat session with tools configured for Automatic Function Calling (AFC)
chat = client.chats.create(
    model="gemini-2.5-flash",
    config=types.GenerateContentConfig(
        tools=tool_list,
        temperature=0.2,
    )
)

prompt = """
Run the Raleigh morning CSR checklist checks for today. 
Fetch business dates first, then query unassigned inspections, prior day incomplete items, and cancellations/not-ready records for Raleigh.
Summarize the findings clearly in an operational report.
"""

print("🚀 Executing Raleigh CSR Agent Test...\n")

response = chat.send_message(prompt)

print(response.text)