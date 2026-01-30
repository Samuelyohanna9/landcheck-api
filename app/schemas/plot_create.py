from pydantic import BaseModel
from typing import List, Optional, Dict

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

class PlotCreateRequest(BaseModel):
    coordinates: List[List[float]]
    stations: Optional[List[Station]] = []
    meta: Optional[PlotMeta] = None
