from pydantic import BaseModel
from typing import List, Optional, Dict, Any

class Station(BaseModel):
    name: str
    lng: float
    lat: float

class PlotMeta(BaseModel):
    title: Optional[str] = None
    location: Optional[str] = None
    lga: Optional[str] = None
    state: Optional[str] = None
    surveyor: Optional[str] = None
    rank: Optional[str] = None
    scale: Optional[str] = "1 : 1000"
    survey_input_coordinates: Optional[List[Dict[str, Any]]] = None

class PlotCreateRequest(BaseModel):
    coordinates: List[List[float]]
    stations: Optional[List[Station]] = []
    meta: Optional[PlotMeta] = None
    # Optional per-attempt id the client generates once and resends unchanged on any retry of the
    # same logical "create this plot" action, so a lost response over a flaky connection can't
    # result in a duplicate plot. Omitted entirely by older/other callers - fully backward compatible.
    client_request_id: Optional[str] = None
