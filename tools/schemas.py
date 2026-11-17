from typing import Optional
from pydantic import BaseModel, Field

class InspectionSearchRequest(BaseModel):
    """Data model representing the request payload for /api/Inspection/SearchInspection."""
    
    # Required core search parameters
    fromDate: str
    thruDate: str
    division: str
    
    # Optional filter parameters (default to None)
    status: Optional[str] = None
    resolution: Optional[str] = None
    builder: Optional[str] = None
    community: Optional[str] = None
    inspector: Optional[str] = None
    superIntendent: Optional[str] = None
    address: Optional[str] = None
    street: Optional[str] = None
    jobNumber: Optional[str] = None
    po: Optional[str] = None
    typeName: Optional[str] = None
    lotAndBlock: Optional[str] = None
    permitNumber: Optional[str] = None
    
    # Default numeric/pagination fields required by the backend API
    invoice: int = 0
    inspectionNumber: int = 0
    addressFilter: int = 0
    locationID: int = 0
    inspectionClass: int = 0
    feeBottom: int = 0
    feeTop: int = 0
    pageNumber: int = 1
    pageSize: int = 500

    def to_api_payload(self) -> dict:
        """
        Converts the model into a clean dictionary, omitting None values 
        so unused optional parameters aren't sent to the backend.
        """
        return self.model_dump(exclude_none=True)