"""Report generation tools — uses HTTP endpoint, NOT XML-RPC.

_render_qweb_pdf is a private method in Odoo 13 (underscore prefix).
XML-RPC blocks private methods. We use the /report/pdf/ HTTP endpoint instead.
"""

import base64
import httpx


def odoo_get_pdf(
    url: str, db: str, user: str, password: str,
    report_name: str,
    record_ids: list[int],
) -> bytes | None:
    """Generate a PDF report from Odoo via HTTP endpoint.

    Args:
        url: Odoo base URL (e.g., https://odoo.empresa.com)
        report_name: Report external ID (e.g., 'sale.action_report_saleorder')
        record_ids: List of record IDs to include in the report

    Returns: PDF bytes or None on failure.
    """
    with httpx.Client(timeout=30) as client:
        # Step 1: Authenticate and get session cookie
        auth_response = client.post(
            f"{url}/web/session/authenticate",
            json={
                "jsonrpc": "2.0",
                "method": "call",
                "params": {"db": db, "login": user, "password": password},
            },
        )
        if auth_response.status_code != 200:
            return None

        # Step 2: Download PDF via report endpoint
        ids_str = ",".join(str(i) for i in record_ids)
        pdf_response = client.get(f"{url}/report/pdf/{report_name}/{ids_str}")

        if pdf_response.status_code == 200 and pdf_response.headers.get("content-type", "").startswith("application/pdf"):
            return pdf_response.content

    return None


def odoo_get_pdf_base64(
    url: str, db: str, user: str, password: str,
    report_name: str,
    record_ids: list[int],
) -> str | None:
    """Get PDF as base64 string (for sending via chat channels)."""
    pdf_bytes = odoo_get_pdf(url, db, user, password, report_name, record_ids)
    if pdf_bytes:
        return base64.b64encode(pdf_bytes).decode("utf-8")
    return None
