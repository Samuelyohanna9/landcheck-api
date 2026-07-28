from fastapi import APIRouter

from app.routers import green


_GREEN_ROUTE_DOMAIN_ORDER = (
    "remote_monitoring",
    "payouts",
    "reports",
    "sponsor",
    "work",
)


def _classify_green_route(path: str) -> str:
    clean_path = str(path or "").strip().lower()
    if clean_path.startswith("/green/remote-monitoring") or clean_path.startswith("/green/vegetation-"):
        return "remote_monitoring"
    if clean_path.startswith("/green/agent-payouts") or clean_path.startswith("/green/admin/sponsor-agent-payout"):
        return "payouts"
    if (
        clean_path.startswith("/green/reports")
        or clean_path.startswith("/green/work-report")
        or clean_path.startswith("/green/export-jobs")
        or "/donor-report/" in clean_path
        or clean_path.endswith("/existing-trees/export/pdf")
        or clean_path.endswith("/existing-trees/export/csv")
        or clean_path.endswith("/custodians/export/pdf")
        or clean_path.endswith("/export/pdf")
        or clean_path.startswith("/green/public/impact/")
    ):
        return "reports"
    if (
        clean_path.startswith("/green/sponsor")
        or clean_path.startswith("/green/sponsorship")
        or clean_path.startswith("/green/public/")
        or clean_path.startswith("/green/merchant-")
        or clean_path.startswith("/green/shop-")
        or clean_path.startswith("/green/track-order")
        or clean_path.startswith("/green/admin/public-projects")
        or clean_path.startswith("/green/admin/sponsor-")
    ):
        return "sponsor"
    return "work"


def build_green_domain_router(domain: str) -> APIRouter:
    clean_domain = str(domain or "").strip().lower()
    if clean_domain not in _GREEN_ROUTE_DOMAIN_ORDER:
        raise ValueError(f"Unsupported green router domain: {domain}")
    router = APIRouter(tags=[f"green-{clean_domain.replace('_', '-')}"])
    for route in green.router.routes:
        route_path = getattr(route, "path", "")
        if _classify_green_route(route_path) == clean_domain:
            router.routes.append(route)
    return router
