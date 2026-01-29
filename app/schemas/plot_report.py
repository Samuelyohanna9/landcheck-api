
#plot_report

class PlotReportOptions(BaseModel):
    title_text: str = "SURVEY PLAN"
    location_text: str = "__________________"
    lga_text: str = "LOCAL GOVERNMENT AREA"
    state_text: str = "STATE"
    station_text: str = "STATION"
    scale_text: str = "1 : 1000"

    surveyor_name: str = "__________________"
    surveyor_rank: str = "__________________"
