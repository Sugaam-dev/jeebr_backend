"""
app/services/invoice_document.py

SentinelOS Carrier-Grade Tax Invoice PDF Generation Engine.
Uses ReportLab to generate publication-quality, statutory-compliant GST Tax Invoices.
Features:
1. Strict Indian GST layout under Rule 46 of CGST Rules, 2017.
2. Itemized SAC 998422 Broadband Internet Telecommunications classification.
3. Accurate Intra-State (CGST + SGST) vs Inter-State (IGST) statutory breakdowns.
4. Preserves fixed-precision rounding and persisted totals from database records.
5. Integration with Document Storage for durable cloud or sandboxed archiving.
"""

import io
import os
from decimal import Decimal
from datetime import datetime
from typing import Optional, Dict, Any

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import inch, mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)
from reportlab.pdfgen import canvas

from app.models import Invoice
from app.services.tax_service import get_supplier_tax_profile


class NumberedCanvas(canvas.Canvas):
    """Two-pass canvas to dynamically compute and print running footer with page count."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748B"))
        # Running Footer
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.5)
        self.line(36, 40, 559, 40)
        self.drawString(36, 28, "SentinelOS Broadband Network • PMRG Solution LLP • Statutory GST Tax Invoice")
        self.drawRightString(559, 28, f"Page {self._pageNumber} of {page_count}")
        self.restoreState()


def generate_invoice_pdf(invoice: Invoice) -> bytes:
    """
    Renders an authoritative Tax Invoice PDF binary for an Invoice record.
    Uses persisted financial data, line items, and dynamic GST tax breakdown.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=54
    )

    styles = getSampleStyleSheet()

    # Custom typography styles
    title_style = ParagraphStyle(
        'InvoiceTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#0F225A")
    )
    subtitle_style = ParagraphStyle(
        'InvoiceSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#2463EB")
    )
    body_style = ParagraphStyle(
        'InvoiceBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#1E293B")
    )
    bold_style = ParagraphStyle(
        'InvoiceBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#0F225A")
    )
    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.white,
        alignment=1  # Center
    )
    table_cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#1E293B")
    )
    table_cell_right = ParagraphStyle(
        'TableCellRight',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#1E293B"),
        alignment=2  # Right
    )

    story = []

    # 1. Header Section
    supplier = get_supplier_tax_profile()
    header_data = [
        [
            Paragraph("<b>SentinelOS</b> Broadband", title_style),
            Paragraph("<b>TAX INVOICE</b>", ParagraphStyle('TaxInvH', parent=title_style, alignment=2, textColor=colors.HexColor("#2463EB")))
        ],
        [
            Paragraph(f"{supplier.legal_name}<br/>{supplier.address}<br/><b>GSTIN:</b> {supplier.gstin} | <b>PAN:</b> {supplier.pan}", body_style),
            Paragraph(f"<b>Invoice No:</b> {invoice.invoice_code}<br/><b>Issue Date:</b> {invoice.created_at.strftime('%d-%b-%Y')}<br/><b>Due Date:</b> {invoice.due_date.strftime('%d-%b-%Y')}<br/><b>Status:</b> {invoice.status.upper()}", ParagraphStyle('MetaRight', parent=body_style, alignment=2))
        ]
    ]
    header_table = Table(header_data, colWidths=[310, 213])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#E2E8F0"), spaceBefore=0, spaceAfter=12))

    # 2. Bill To & Supply Details
    cust = invoice.customer
    cust_name = cust.name if cust else "Valued Subscriber"
    cust_code = cust.customer_code if cust else f"CUST-{invoice.customer_id}"
    cust_email = cust.email if cust else "N/A"
    cust_phone = cust.phone if cust else "N/A"
    cust_locality = getattr(cust, 'locality', 'Mumbai Area')
    pos = getattr(invoice, 'place_of_supply', None) or ("27-Maharashtra" if invoice.market_id.lower() == "mumbai" else "19-West Bengal")
    tax_treatment = getattr(invoice, 'tax_treatment', 'INTRA_STATE')

    billing_info_data = [
        [
            Paragraph("<b>BILLED TO (SUBSCRIBER)</b>", subtitle_style),
            Paragraph("<b>TAX & SUPPLY SPECIFICATIONS</b>", subtitle_style)
        ],
        [
            Paragraph(
                f"<b>Name:</b> {cust_name}<br/>"
                f"<b>Subscriber ID:</b> {cust_code}<br/>"
                f"<b>Billing Address:</b> {cust_locality}, {invoice.market_id.capitalize()}<br/>"
                f"<b>Email:</b> {cust_email} | <b>Phone:</b> {cust_phone}",
                body_style
            ),
            Paragraph(
                f"<b>Place of Supply:</b> {pos}<br/>"
                f"<b>Supply Treatment:</b> {tax_treatment.replace('_', ' ').title()}<br/>"
                f"<b>Billing Period:</b> {invoice.billing_period_start.strftime('%d-%b-%Y') if invoice.billing_period_start else 'Current'} to {invoice.billing_period_end.strftime('%d-%b-%Y') if invoice.billing_period_end else 'Next Cycle'}<br/>"
                f"<b>SAC Code:</b> 998422 (Internet Telecom)",
                body_style
            )
        ]
    ]
    bill_table = Table(billing_info_data, colWidths=[270, 253])
    bill_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
    ]))
    story.append(bill_table)
    story.append(Spacer(1, 16))

    # 3. Itemized Charges Table
    is_intra = (tax_treatment == "INTRA_STATE")
    table_headers = [
        Paragraph("#", table_header_style),
        Paragraph("Description & Plan", table_header_style),
        Paragraph("SAC", table_header_style),
        Paragraph("Qty", table_header_style),
        Paragraph("Unit (INR)", table_header_style),
        Paragraph("Taxable (INR)", table_header_style),
        Paragraph("CGST 9%" if is_intra else "IGST 18%", table_header_style),
        Paragraph("SGST 9%" if is_intra else "Cess", table_header_style),
        Paragraph("Total (INR)", table_header_style),
    ]

    items_data = [table_headers]

    subtotal_dec = Decimal(str(invoice.subtotal))
    tax_dec = Decimal(str(invoice.tax_amount))
    cgst_dec = Decimal(str(getattr(invoice, 'cgst_amount', '0.00')))
    sgst_dec = Decimal(str(getattr(invoice, 'sgst_amount', '0.00')))
    igst_dec = Decimal(str(getattr(invoice, 'igst_amount', '0.00')))
    total_dec = Decimal(str(invoice.total_amount))

    # If cgst/sgst not set on old records, fallback cleanly
    if is_intra and cgst_dec == Decimal('0.00') and tax_dec > Decimal('0.00'):
        cgst_dec = (tax_dec / Decimal('2.00')).quantize(Decimal('0.01'))
        sgst_dec = tax_dec - cgst_dec
    elif not is_intra and igst_dec == Decimal('0.00') and tax_dec > Decimal('0.00'):
        igst_dec = tax_dec

    if invoice.line_items:
        for idx, item in enumerate(invoice.line_items, 1):
            i_tax = Decimal(str(item.tax_amount))
            i_cgst = (i_tax / Decimal('2.00')).quantize(Decimal('0.01')) if is_intra else Decimal('0.00')
            i_sgst = (i_tax - i_cgst) if is_intra else Decimal('0.00')
            i_igst = i_tax if not is_intra else Decimal('0.00')

            items_data.append([
                Paragraph(str(idx), table_cell_style),
                Paragraph(item.description, table_cell_style),
                Paragraph("998422", table_cell_style),
                Paragraph(f"{float(item.quantity):.1f}", table_cell_style),
                Paragraph(f"{float(item.unit_price):,.2f}", table_cell_right),
                Paragraph(f"{float(item.amount):,.2f}", table_cell_right),
                Paragraph(f"{float(i_cgst if is_intra else i_igst):,.2f}", table_cell_right),
                Paragraph(f"{float(i_sgst if is_intra else Decimal('0.00')):,.2f}", table_cell_right),
                Paragraph(f"{float(item.total_amount):,.2f}", table_cell_right),
            ])
    else:
        items_data.append([
            Paragraph("1", table_cell_style),
            Paragraph(f"Broadband Subscription: {invoice.plan_name}", table_cell_style),
            Paragraph("998422", table_cell_style),
            Paragraph("1.0", table_cell_style),
            Paragraph(f"{float(subtotal_dec):,.2f}", table_cell_right),
            Paragraph(f"{float(subtotal_dec):,.2f}", table_cell_right),
            Paragraph(f"{float(cgst_dec if is_intra else igst_dec):,.2f}", table_cell_right),
            Paragraph(f"{float(sgst_dec if is_intra else Decimal('0.00')):,.2f}", table_cell_right),
            Paragraph(f"{float(total_dec):,.2f}", table_cell_right),
        ])

    items_table = Table(items_data, colWidths=[20, 160, 42, 28, 55, 60, 52, 52, 54])
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#0F225A")),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 14))

    # 4. Summary & Ledger Balancing Block
    paid_dec = Decimal(str(invoice.paid_amount))
    balance_dec = Decimal(str(invoice.outstanding_amount))

    summary_data = [
        [Paragraph("<b>Subtotal (Taxable Value):</b>", body_style), Paragraph(f"INR {float(subtotal_dec):,.2f}", table_cell_right)],
        [Paragraph("<b>Promotional Discount:</b>", body_style), Paragraph(f"- INR {float(invoice.discount_amount):,.2f}", table_cell_right)],
    ]
    if is_intra:
        summary_data.append([Paragraph("<b>Central GST (CGST 9.00%):</b>", body_style), Paragraph(f"INR {float(cgst_dec):,.2f}", table_cell_right)])
        summary_data.append([Paragraph("<b>State GST (SGST 9.00%):</b>", body_style), Paragraph(f"INR {float(sgst_dec):,.2f}", table_cell_right)])
    else:
        summary_data.append([Paragraph("<b>Integrated GST (IGST 18.00%):</b>", body_style), Paragraph(f"INR {float(igst_dec):,.2f}", table_cell_right)])

    summary_data.extend([
        [Paragraph("<b>Total Invoice Amount:</b>", bold_style), Paragraph(f"<b>INR {float(total_dec):,.2f}</b>", ParagraphStyle('TB', parent=table_cell_right, fontName='Helvetica-Bold'))],
        [Paragraph("Amount Paid / Settled:", body_style), Paragraph(f"INR {float(paid_dec):,.2f}", table_cell_right)],
        [Paragraph("<b>Net Balance Due:</b>", bold_style), Paragraph(f"<b>INR {float(balance_dec):,.2f}</b>", ParagraphStyle('NB', parent=table_cell_right, fontName='Helvetica-Bold', textColor=colors.HexColor("#B91C1C") if balance_dec > Decimal('0.00') else colors.HexColor("#15803D")))],
    ])

    summary_table = Table(summary_data, colWidths=[150, 100])
    summary_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, 3 if is_intra else 2), (-1, 3 if is_intra else 2), 0.5, colors.HexColor("#CBD5E1")),
        ('LINEBELOW', (0, -2), (-1, -2), 0.5, colors.HexColor("#CBD5E1")),
    ]))

    # Payment Notice Block
    status_color = "#15803D" if invoice.status.upper() == "PAID" else ("#B91C1C" if invoice.status.upper() == "OVERDUE" else "#2563EB")
    notice_text = (
        f"<font color='{status_color}'><b>PAYMENT STATUS: {invoice.status.upper()}</b></font><br/>"
        f"UPI ID for Instant Autopay: <b>sentinelos.pmrg@icici</b><br/>"
        f"NEFT/RTGS Account: 000205001234 (IFSC: ICIC0000002)<br/>"
        f"Thank you for choosing SentinelOS Fiber Broadband."
    )
    notice_table = Table([
        [Paragraph(notice_text, body_style), summary_table]
    ], colWidths=[263, 260])
    notice_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BACKGROUND', (0, 0), (0, 0), colors.HexColor("#F8FAFC")),
        ('BOX', (0, 0), (0, 0), 0.5, colors.HexColor("#CBD5E1")),
        ('LEFTPADDING', (0, 0), (0, 0), 8),
        ('RIGHTPADDING', (0, 0), (0, 0), 8),
        ('TOPPADDING', (0, 0), (0, 0), 8),
        ('BOTTOMPADDING', (0, 0), (0, 0), 8),
    ]))
    story.append(notice_table)

    story.append(Spacer(1, 18))
    statutory_note = (
        "<b>Statutory Notes & Legal Compliance:</b><br/>"
        "1. This document is a digitally issued, computer-generated Tax Invoice conforming to Rule 46 of the CGST Rules, 2017.<br/>"
        "2. Service Category: Wired Broadband Internet Telecommunications Service (SAC: 998422).<br/>"
        "3. Reverse Charge Mechanism: NO. Supply is subject to forward charge under Section 9(1) of the CGST Act, 2017."
    )
    story.append(Paragraph(statutory_note, ParagraphStyle('LegalNote', parent=body_style, fontSize=7, leading=9.5, textColor=colors.HexColor("#64748B"))))

    # Build document
    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()
