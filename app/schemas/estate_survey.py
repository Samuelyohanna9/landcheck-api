from pydantic import BaseModel

class SurveyorAssignment(BaseModel):
    subject_type: str
    subject_id: str
