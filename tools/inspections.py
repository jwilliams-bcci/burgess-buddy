# tools/inspections.py
from typing import Optional
from .base_client import InternalPortalClient
from .schemas import InspectionSearchRequest

portal_client = InternalPortalClient()

KEYS_TO_KEEP = {
    "inspectionID", "address", "street", "builder", 
    "community", "status", "resolution", "fee", 
    "inspector", "typeName", "specialNotes"
}

def search_inspections(
    division: str,
    from_date: str,
    thru_date: str,
    status: Optional[str] = None,
    resolution: Optional[str] = None,
    builder: Optional[str] = None,
    community: Optional[str] = None
) -> list[dict]:
    """
    Executes a search query against the internal portal inspection database.
    Accepts structured filters like division, date ranges, status, builder, or resolution.
    
    Args:
        division (str): Division name (e.g., 'Dallas' or 'Raleigh').
        from_date (str): Start date in 'MM-DD-YYYY' or 'YYYY-MM-DD' format.
        thru_date (str): End date in 'MM-DD-YYYY' or 'YYYY-MM-DD' format.
        status (str, optional): Filter by status (e.g., 'Assigned', 'Unassigned', 'Complete').
        resolution (str, optional): Filter by resolution (e.g., 'Cancelled in', 'Not Ready').
        builder (str, optional): Filter by builder name.
        community (str, optional): Filter by community name.
    """
    search_req = InspectionSearchRequest(
        division=division,
        fromDate=from_date,
        thruDate=thru_date,
        status=status,
        resolution=resolution,
        builder=builder,
        community=community
    )

    response = portal_client.post(
        "Inspection/SearchInspection", 
        json_data=search_req.to_api_payload()
    )

    if isinstance(response, dict) and "error" in response:
        return [response]

    raw_items = response.get("searchResponse", [])

    return [
        {k: item[k] for k in KEYS_TO_KEEP if k in item}
        for item in raw_items
    ]